import site
site.addsitedir('vendor')
site.addsitedir('vendor/lib/python')

try:
    import simplejson as json
except ImportError:
    import json
import sys, os, traceback, urllib2, urllib, re
from time import time, strftime, strptime, localtime, mktime, gmtime
from argparse import ArgumentParser
from utils.db_handler import DBHandler
import ConfigParser
import utils.bz_utils as bz_utils
import utils.mq_utils as mq_utils
import logging, logging.handlers
from mercurial import lock, error
import requests

# sets up a rotating logfile that's written to the working dir
log = logging.getLogger()
FORMAT = "%(asctime)s - %(module)s - %(funcName)s - %(message)s"
POLLING_INTERVAL = 14400 # 4 hours
TIMEOUT = 43200 # 12 hours
MAX_POLLING_INTERVAL = 172800 # 48 hours
COMPLETION_THRESHOLD = 600 # 10 minutes
MAX_ORANGE = 10
LOCK_FILE_PATH = '/tmp.schedulerDbPoller.lock'

# console logging, formatted
logging.basicConfig(format=FORMAT)

class SchedulerDBPoller():

    def __init__(self, branch, cache_dir, configs,
                user=None, password=None, dry_run=False,
                verbose=False, messages=True):

        self.config = ConfigParser.ConfigParser()
        self.config.read(configs)
        self.branch = branch
        self.cache_dir = cache_dir
        self.dry_run = dry_run
        self.verbose = verbose
        self.messages = messages
        self.posted_bugs = self.config.get('log', 'posted_bugs')

        # Set up the message queue
        if self.messages:
            self.mq = mq_utils.mq_util(host=self.config.get('mq', 'host'),
                    vhost=self.config.get('mq', 'vhost'),
                    username=self.config.get('mq', 'username'),
                    password=self.config.get('mq', 'password'),
                    exchange=self.config.get('mq', 'exchange'))
            self.mq.connect()

        # Set up bugzilla api connection
        self.bz_url = self.config.get('bz', 'url')
        self.bz = bz_utils.bz_util(self.config.get('bz', 'api_url'),
                self.config.get('bz', 'url'),
                None, self.config.get('bz', 'username'),
                self.config.get('bz', 'password'))

        # Set up Self-Serve API
        self.self_serve_api_url = self.config.get('self_serve', 'url')
        self.user = self.config.get('self_serve', 'user')
        self.password = self.config.get('self_serve', 'password')

        # Set up database handler
        self.scheduler_db = DBHandler(
                self.config.get('databases', 'scheduler_db_url'))

    def revisionTimedOut(self, revision, timeout=TIMEOUT):
        """
        Read the cache file for revision and return if the build has timed out
        """
        timed_out = False
        now = time()
        log.debug("Checking for timed out revision: %s" % revision)
        filename = os.path.join(self.cache_dir, revision)
        if os.path.exists(filename):
            try:
                f = open(filename, 'r')
                entries = f.readlines()
                f.close()
            except IOError, e:
                log.error("Couldn't open cache file for rev: %s" % revision)
                raise
            try:
                first_entry = mktime(strptime(
                        entries[0].split('|')[0], "%a, %d %b %Y %H:%M:%S %Z"))
            except OverflowError, ValueError:
                log.error("Could not convert time %s to localtime"
                            % (entries[0].split('|')[0]))
                return False    # can't say it's timed out
            diff = now - first_entry
            if diff > timeout:
                log.debug("Timeout on rev: %s " % revision)
                timed_out = True
        return timed_out

    def OrangeFactorHandling(self, buildrequests, max_orange=MAX_ORANGE):
        """
        Checks buildrequests results.
        If all success except # warnings is <= MAX_ORANGE
            * Check if the buildername with warning result is
              duplicated in requests
            * If not, triggers a rebuild using self-serve API of that
              buildernames's buildid
            * If yes, check the results of the pair and report back
              success/fail based on:
                orange:green == Success, intermittent orange
                orange:orange == Failed on retry

        returns:
            is_complete {True,False}
            final_status {'SUCCESS', 'FAILURE', 'RETRYING', None}
        """
        is_complete = False
        final_status = None
        results = self.CalculateResults(buildrequests)

        log.debug("RESULTS (OrangeFactorHandling): %s" % results)
        if results['failure'] or results['other'] \
                or results['skipped'] or results['exception']:
            log.debug("Complete, at least one build failed.")
            is_complete = True
            final_status = "FAILURE"
        elif results['success'] == results['total_builds']:
            log.debug("Complete, all builds successful.")
            is_complete = True
            final_status = "SUCCESS"
        elif results['warnings'] <= max_orange:
            log.debug("Complete: %d warnings < threshold %d."
                    % (results['warnings'], max_orange))
            is_complete = True
            final_status = "SUCCESS"
        elif results['total_builds'] == results['success'] + results['warnings']:
            log.debug("Have warnings. Check if it's a retry.")
            buildernames = {}
            for value in buildrequests.values():
                br = value.to_dict()
                # Collect duplicate buildernames
                if not buildernames.has_key(br['buildername']):
                    buildernames[br['buildername']] = \
                            [(br['results_str'], br['branch'], br['bid'])]
                else:
                    buildernames[br['buildername']].append(
                            (br['results_str'], br['branch'], br['bid']))

            duplicates = []
            for name, info in buildernames.items():
                # collect all duplicates
                if len(info) > 1:
                    log.debug("We have a duplicate: %s" % (name))
                    duplicates.append(info)

            retry_count = len(duplicates)

            if retry_count*2 >= results['warnings']:
                log.debug("Finished retry run")
                is_complete = True
                # Remove all initial retried warnings
                if results['warnings'] - retry_count <= max_orange:
                    final_status = "SUCCESS"
                else:
                    final_status = "FAILURE"
            elif results['warnings'] > max_orange:
                log.debug("Over max orange, trigger retries")
                # attempt rebuilds
                for info in buildernames.values():
                    for (result, branch, bid) in info:
                        if result == 'warnings':
                            log.debug("Attempting to retry branch: "
                                        "%s bid: %s" % (branch, bid))
                            try:
                                post = self.SelfServeRebuild(bid)
                                is_complete = False
                                final_status = "RETRYING"
                            except (urllib2.HTTPError, ValueError), err:
                                log.warn("Unable to retry. Exception: %s" % (err))
                                is_complete = True
                                final_status = "FAILURE"

        else:
            log.error("Getting invalid values from schedulerDB")
            is_complete = True
            final_status = "FAILURE"

        return is_complete, final_status

    def SelfServeRebuild(self, buildid):
        """
        Uses self-serve API to retrigger the buildid/branch sent in
        with a POST request
        """
        auth = (self.user, self.password)
        headers = { 'Accepts' : 'application/json' }
        data = { "build_id": buildid }
        url = "%s/%s/build" % (self.self_serve_api_url, self.branch)
        if self.dry_run:
           log.debug("Would retry\n\tbuild_id: %s"
                     "\n\turl: %s\n\tdata: %s" % (buildid, url, data))
           return None
        try:
            req = requests.post(url, auth=auth, headers=headers, data=data)
            if req.status_code != 302:
                req.raise_for_status()
            else:
                return json.loads(req.content)
        except urllib2.HTTPError, e:
            log.warn("FAIL attempted rebuild for %s:%s -- %s"
                    % (self.branch, buildid, e))
            raise
        except ValueError, err:
            log.warn("FAILED to load json result for %s:%s -- %s"
                    % (self.branch, buildid, err))
            raise

    def GetSingleAuthor(self, buildrequests):
        """
        Look through a list of buildrequests and return only one author
        from the changes if one exists
        """
        author = None
        for value in buildrequests.values():
            br = value.to_dict()
            if author == None:
                author = br['authors']
        # if there's one author return it
        if len(author) == 1:
            return author[0]
        elif author:
            log.error("More than one author for: %s" % br)
        return None

    def GetBugNumbers(self, buildrequests):
        """
        Look through a list of buildrequests and return bug
        number from push comments
        """
        bugs = []
        for value in buildrequests.values():
            br = value.to_dict()
            for comment in br['comments']:
                # we only want the bug specified in try syntax
                if len(comment.split('try: ')) > 1:
                    comment = comment.split('try: ')[1]
                    bugs = self.bz.bugs_from_comments(comment)
        return bugs

    def ProcessPushType(self, revision, buildrequests, flag_check=True):
        """
        Search buildrequest comments for try syntax and query autoland_db
        returns type as "TRY", "RETRY", or None
            try: if "try: --post-to-bugzilla" is present in the
                 comments of a buildrequest
            retry: if "try: --retry-oranges" is present in the
                 comments of a buildrequest
            None: if not try request and Autoland system isn't tracking it
        """
        push_type = None
        max_orange = MAX_ORANGE
        for value in buildrequests.values():
            br = value.to_dict()
            for comments in br['comments']:
                if 'try: ' in comments:
                    if flag_check:
                        if '--post-to-bugzilla' in comments:
                            push_type = "TRY"
                    else:
                        push_type = "TRY"
                    if '--retry-oranges' in comments:
                        push_type = "RETRY"
                        # eliminate any empty strings from the split
                        max_orange = [x for x in
                                comments.split('--retry-oranges') if x]
                        if not len(max_orange) > 1:
                            max_orange = MAX_ORANGE
                            continue
                        max_orange = max_orange[1].split()[0]
                        try:
                            max_orange = int(max_orange)
                        except ValueError:
                            max_orange = MAX_ORANGE
        return push_type, max_orange

    def CalculateResults(self, buildrequests):
        """
        Returns dictionary of the results for the buildrequests passed in
        """
        results = {
            'success': 0,
            'warnings': 0,
            'failure': 0,
            'skipped': 0,
            'exception': 0,
            'other': 0,
            'total_builds': 0
        }
        for value in buildrequests.values():
            br = value.to_dict()
            # Do the tallying of statuses
            if br['results_str'].lower() in results.keys():
                results[br['results_str'].lower()] += 1
            else:
                results['other'] += 1
        results['total_builds'] = sum(results.values())
        return results

    def GenerateResultReportMessage(self, revision, report, author=None):
        """ Returns formatted message of revision report"""
        if self.verbose:
            log.debug("REPORT: %s" % report)

        tree = self.branch.title()
        if 'comm' in self.branch:
            tree = "Thunderbird-Try"

        message = "Try run for %s is complete.\n" \
                  "Detailed breakdown of the results available here:\n" \
                  "\thttps://tbpl.mozilla.org/?tree=%s&rev=%s\n" \
                  "Results (out of %d total builds):\n" \
                  % (revision, tree,
                     revision, report['total_builds'])
        for key, value in report.items():
            if value > 0 and key != 'total_builds':
                message += "    %s: %d\n" % (key, value)
        if author:
            app = 'firefox'
            if 'comm' in self.branch:
                app = 'thunderbird'
            message += "Builds (or logs if builds failed) available at:\n"\
                        "http://ftp.mozilla.org/pub/mozilla.org/%s/"\
                        "try-builds/%s-%s""" % (app, author, revision)
        return message

    def WriteToBuglist(self, revision, bug):
        """
        Writes a bug #, timestamp, and build's info to the BUGLIST to
        track what has been posted
        Also calls RemoveCache on the revision once it's been posted
        """
        if self.dry_run:
            log.debug("DRY_RUN: WRITING TO %s: %s" % (self.posted_bugs, revision))
        else:
            try:
                f = open(self.posted_bugs, 'a')
                f.write("%s|%s|%d|%s\n" % (bug, revision, time(),
                    strftime("%a, %d %b %Y %H:%M:%S %Z", localtime())))
                f.close()
                log.debug("WROTE TO %s: %s" % (self.posted_bugs, revision))
                self.RemoveCache(revision)
            except:
                traceback.print_exc(file=sys.stdout)

    def RemoveCache(self, revision):
        # attach '.done' to the cache file so we're not tracking it anymore
        # delete original cache file
        cache_file = os.path.join(self.cache_dir, revision)
        log.debug("MOVING %s CACHE FILE to %s"
                % (cache_file, cache_file + '.done'))
        if os.path.exists(cache_file):
            os.rename(cache_file, cache_file + '.done')

    def LoadCache(self):
        """
        Search for cache dir, return dict of all filenames (revisions)
        in the dir and a list of completed revisions to knock out of poll run
        """
        revisions = {}
        completed_revisions = []
        log.debug("Scanning cache files...")
        if os.path.isdir(self.cache_dir):
            cache_revs = os.listdir(self.cache_dir)
            for revision in cache_revs:
                if '.done' in revision:
                    completed_revisions.append(revision.split('.')[0])
                else:
                    revisions[revision] = {}

        return revisions, completed_revisions

    def WriteToCache(self, incomplete):
        """
        Writes results of incomplete build to cache dir in a file that
        is named with the revision
        """
        try:
            assert isinstance(incomplete, dict)
        except AssertionError:
            log.error("Incomplete should be type:dict")
            raise

        if not os.path.isdir(self.cache_dir):
            if not self.dry_run:
                os.mkdir(self.cache_dir)
                if self.verbose:
                    log.debug("CREATED DIR: %s" % self.cache_dir)
            else:
                log.debug("DRY RUN: WOULD CREATE DIR: %s" % self.cache_dir)

        for revision, results in incomplete.items():
            filename = os.path.join(self.cache_dir, revision)
            if self.dry_run:
                log.debug("DRY RUN: WOULD WRITE TO %s: %s|%s\n"
                        % (filename, strftime("%a, %d %b %Y %H:%M:%S %Z",
                           localtime()), results))
            else:
                try:
                    f = open(filename, 'a')
                    f.write("%s|%s\n" % (strftime("%a, %d %b %Y %H:%M:%S %Z",
                        localtime()), results))
                    if self.verbose:
                        log.debug("WROTE TO %s: %s|%s\n"
                                % (filename,
                                   strftime("%a, %d %b %Y %H:%M:%S %Z",
                                   localtime()), results))
                    f.close()
                except:
                    log.error(traceback.print_exc(file=sys.stdout))
                    raise

    def CalculateBuildRequestStatus(self, buildrequests,
            revision=None, retry_oranges=False, max_orange=MAX_ORANGE):
        """
        Accepts buildrequests and calculates their results, calls
        orange_factor_handling to ensure completeness of results, makes sure
        that COMPLETION_THRESHOLD is met before declaring a build finished
        (this is for delays in test triggerings)

        If a revision is passed in, the revision will be checked for timeout in
        revision_timed_out and factored into the is_complete value

        returns a tuple of:
            status (dict)
            is_complete (boolean)
        """
        is_complete = False
        status = {
            'total_builds': 0,
            'pending': 0,
            'running': 0,
            'complete': 0,
            'cancelled': 0,
            'interrupted': 0,
            'misc': 0,
            'status_string': "",
        }
        for value in buildrequests.values():
            status['total_builds'] +=1
            br = value.to_dict()
            if br['status_str'].lower() in status.keys():
                status[br['status_str'].lower()] += 1

        total_complete = status['misc'] + status['interrupted'] + \
                status['cancelled'] + status['complete']
        if status['total_builds'] == total_complete:
            is_complete = True
            timeout_complete = []
            for value in buildrequests.values():
                br = value.to_dict()
                if br['finish_time']:
                    timeout_complete.append(
                            time() - br['finish_time'] > COMPLETION_THRESHOLD)
                for passed_timeout in timeout_complete:
                    if not passed_timeout:
                        # we'll wait a bit and make sure no tests are coming
                        is_complete = False
                        break
            if is_complete and retry_oranges:
                # Only want to check orange factor if we are retrying oranges
                log.debug("Check Orange Factor for rev: %s" % revision)
                is_complete, status['status_string'] = \
                        self.OrangeFactorHandling(buildrequests, max_orange)
        # check timeout, maybe it's time to kick this out of the tracking queue
        if revision != None:
            if self.revisionTimedOut(revision):
                status['status_string'] = 'TIMED_OUT'
                is_complete = True

        return (status,is_complete)

    def GetRevisions(self, starttime=None, endtime=None):
        """
        Gets the buildrequests between starttime & endtime,
        returns a dict keyed by revision with the buildrequests per revision
        """
        rev_dict = {}
        buildrequests = self.scheduler_db.GetBuildRequests(None, self.branch,
                starttime, endtime)
        for value in buildrequests.values():
            # group buildrequests by revision
            br = value.to_dict()
            revision = br['revision']
            if not rev_dict.has_key(revision):
                rev_dict[revision] = {}
        return rev_dict

    def ProcessCompletedRevision(self, revision, message,
                                 bug, status_str, run_type):
        """
        Posts to bug and sends msg to autoland mq with completion status
        """
        bug_post = False
        dupe = False
        result = False
        action = run_type + '.RUN'

        if status_str == 'TIMED_OUT':
            message += "\n Timed out after %s hours without completing." \
                    % strftime('%I', gmtime(TIMEOUT))

        posted = self.bz.has_comment(message, bug)

        if posted:
            log.debug("NOT POSTING TO BUG %s, ALREADY POSTED" % bug)
            dupe = True
            self.RemoveCache(revision)
        else:
            if self.dry_run:
                log.debug("DRY_RUN: Would post to %s%s" % (self.bz_url, bug))
            else:
                log.debug("Type: %s Revision: %s - "
                        "bug comment & message being sent"
                        % (run_type, revision))
                result = self.bz.notify_bug(message, bug)
        if result:
            self.WriteToBuglist(revision, bug)
            log.debug("BZ POST SUCCESS result: %s bug: %s%s"
                    % (result, self.bz_url, bug))
            bug_post = True
            if self.messages:
                msg = { 'type'  : status_str,
                        'action': action,
                        'bug_id' : bug,
                        'revision': revision }
                self.mq.send_message(msg, routing_key='db')

        elif not self.dry_run and not dupe:
            # Still can't post to the bug even on time out?
            # Throw it away for now (maybe later we'll email)
            if status_str == 'TIMED_OUT' and not result:
                self.RemoveCache(revision)
            else:
                log.debug("BZ POST FAILED message: %s bug: %s, "
                          "couldn't notify bug. Try again later."
                          % (message, bug))
        return bug_post

    def PollByRevision(self, revision, flag_check=False):
        """
        Run a single revision through the polling process to determine if it is
        complete, or not, returns information on the revision in a dict which
        includes the message that can be posted to a bug
        (if not in dryrun mode), whether the message was successfully posted,
        and the current status of the builds
        """
        info = {
            'message': None,
            'posted_to_bug': False,
            'status': None,
            'is_complete': False,
            'discard': False,
        }
        buildrequests = self.scheduler_db.GetBuildRequests(revision, self.branch)
        run_type, max_orange = self.ProcessPushType(
                revision, buildrequests, flag_check)
        bugs = self.GetBugNumbers(buildrequests)
        info['status'], info['is_complete'] = \
                self.CalculateBuildRequestStatus(buildrequests,
                        revision,
                        retry_oranges=(run_type == "RETRY"),
                        max_orange=max_orange)
        log.debug("POLL_BY_REVISION: RESULTS: %s BUGS: %s TYPE: "
                  "%s IS_COMPLETE: %s"
                  % (info['status'], bugs, type, info['is_complete']))
        log.debug("POLL_BY_REVISION: RESULTS: %s BUGS: %s "
                "TYPE: %s IS_COMPLETE: %s"
                % (info['status'], bugs, run_type, info['is_complete']))
        if info['is_complete'] and len(bugs) > 0:
            results = self.CalculateResults(buildrequests)
            info['message'] = self.GenerateResultReportMessage(
                    revision, results, self.GetSingleAuthor(buildrequests))
            if self.verbose:
                log.debug("POLL_BY_REVISION: MESSAGE: %s" % info['message'])
            for bug in bugs:
                if info['message'] != None and self.dry_run == False:
                    info['posted_to_bug'] = self.ProcessCompletedRevision(
                            revision=revision, message=info['message'],
                            bug=bug, run_type=run_type,
                            status_str=info['status']['status_string'])
                elif self.dry_run:
                    log.debug("DRY RUN: Would have posted %s to %s"
                            % (info['message'], bug))
        # No bug number(s) or no try syntax
        # complete still gets flagged for discard
        elif info['is_complete']:
            log.debug("Nothing to do here for %s" % revision)
            info['discard'] = True
        else:
            if bugs != None and not self.dry_run:
                # Cache it
                log.debug("Writing %s to cache" % revision)
                incomplete = {}
                incomplete[revision] = info['status']
                self.WriteToCache(incomplete)
            else:
                info['discard'] = True
        return info

    def PollByTimeRange(self, starttime, endtime):
        # Get all the unique revisions in the specified timeframe range
        rev_report = self.GetRevisions(starttime,endtime)
        # Check the cache for any additional revisions to pull reports for
        revisions, completed_revisions = self.LoadCache()
        log.debug("INCOMPLETE REVISIONS IN CACHE %s" % (revisions))
        rev_report.update(revisions)
        # Clear out complete revisions from the rev_report keys
        for rev in completed_revisions:
            if rev_report.has_key(rev):
                log.debug("Removing %s from the revisions to poll, "
                          "it's been done." % rev)
                del rev_report[rev]

        # Check each revision's buildrequests to determine: completeness, type
        for revision in rev_report.keys():
            buildrequests = self.scheduler_db.GetBuildRequests(
                    revision, self.branch)
            rev_report[revision]['bugs'] = self.GetBugNumbers(buildrequests)
            rev_report[revision]['push_type'], max_orange = \
                    self.ProcessPushType(revision, buildrequests)
            (rev_report[revision]['status'],
             rev_report[revision]['is_complete']) = \
                     self.CalculateBuildRequestStatus(buildrequests,
                             revision,
                             retry_oranges=(rev_report[revision]['push_type'] == "RETRY"),
                             max_orange=max_orange)

            # For completed runs, generate a bug comment message if necessary
            if rev_report[revision]['is_complete'] and \
                    len(rev_report[revision]['bugs']) > 0:
                rev_report[revision]['results'] = \
                        self.CalculateResults(buildrequests)
                rev_report[revision]['message'] = \
                        self.GenerateResultReportMessage(revision,
                                rev_report[revision]['results'],
                                self.GetSingleAuthor(buildrequests))
            else:
                rev_report[revision]['message'] = None

        # Process the completed rev_report for this run
        # gather incomplete revisions and writing to cache
        incomplete = {}
        for revision, info in rev_report.items():
            # Incomplete builds that have bugs
            # get added to dict for re-checking later
            if not info['is_complete']:
                if len(info['bugs']) == 1:
                    incomplete[revision] = {'status': info['status'],
                                            'bugs': info['bugs'],
                                            }

            # Try syntax has --post-to-bugzilla so we want to post to bug
            if info['is_complete'] and \
                    info['push_type'] != None and len(info['bugs']) == 1:
                bug = info['bugs'][0]
                if not self.ProcessCompletedRevision(revision,
                          rev_report[revision]['message'],
                          bug,
                          rev_report[revision]['status']['status_string'],
                          info['push_type']):
                    # If bug post didn't happen put it back
                    # (once per revision) into cache to try again later
                    if not incomplete.has_key(revision):
                        incomplete[revision] = {'status': info['status'],
                                                'bugs': info['bugs']}
            # Complete but to be discarded
            elif info['is_complete']:
                if self.verbose:
                    log.debug("Nothing to do for push_type:%s revision:%s - "
                              "no one cares about it"
                              % (info['push_type'], revision))
                self.RemoveCache(revision)
        # Clean incomplete list of timed out build runs
        for rev in incomplete.keys():
            if incomplete[rev]['status']['status_string'] == 'TIMED_OUT':
                del incomplete[rev]

        # Store the incomplete revisions for the next run if there's a bug
        self.WriteToCache(incomplete)

        return incomplete

if __name__ == '__main__':
    """
    Poll the schedulerdb for all the buildrequests of a certain timerange or a
    single revision. Determine the results of that revision/timerange's
    buildsets and then posts to the bug with results for any that are complete
    (if it's a try-syntax push, then checks for --post-to-bugzilla flag). Any
    revision(s) builds that are not complete are written to a cache file named
    by revision for checking again later.
    """

    parser = ArgumentParser()
    parser.add_argument("-b", "--branch",
                        dest="branch",
                        help="the branch revision to poll",
                        required=True)
    parser.add_argument("-c", "--config-file",
                        dest="configs",
                        action="append",
                        help="config file to use for accessing db",
                        required=True)
    parser.add_argument("-r", "--revision",
                        dest="revision",
                        help="a specific revision to poll")
    parser.add_argument("-s", "--start-time",
                        dest="starttime",
                        help="unix timestamp to start polling from")
    parser.add_argument("-e", "--end-time",
                        dest="endtime",
                        help="unix timestamp to poll until")
    parser.add_argument("-n", "--dry-run",
                        dest="dry_run",
                        help="flag to turn off actually posting to bugzilla",
                        action='store_true')
    parser.add_argument("-v", "--verbose",
                        dest="verbose",
                        help="turn on verbose output",
                        action='store_true')
    parser.add_argument("--cache-dir",
                        dest="cache_dir",
                        help="working dir for tracking incomplete revisions")
    parser.add_argument("--no-messages",
                        dest="messages",
                        help="toggle for sending messages to queue",
                        action='store_false')
    parser.add_argument("--flag-check",
                        dest="flag_check",
                        help="toggle for checking if --post-to-bugzilla "\
                             "is in the build's comments",
                        action='store_true')
    parser.add_argument("-l", "--log-file",
                        dest="log_file",
                        help="specify the file path to log to.")
    parser.set_defaults(
        branch="try",
        cache_dir="cache",
        revision=None,
        starttime = time() - POLLING_INTERVAL,
        endtime = time(),
        dry_run = False,
        messages = True,
        flag_check = False,
        log_file = None,
    )

    options, args = parser.parse_known_args()

    for config in options.configs:
        if not os.path.exists(config):
            log.error("Config file %s does not exist or is not valid."
                        % config)
            sys.exit(1)

    lock_file = None
    try:
        lock_file = lock.lock(LOCK_FILE_PATH timeout=1)

        # set up logging
        if not options.log_file:
            # log to stdout
            handler = logging.StreamHandler()
        else:
            handler = logging.handlers.RotatingFileHandler(options.log_file,
                            maxBytes=50000, backupCount=5)
        if not options.verbose:
            log.setLevel(logging.INFO)
        else:
            log.setLevel(logging.DEBUG)
        log.addHandler(handler)


        if options.revision:
            poller = SchedulerDBPoller(branch=options.branch,
                    cache_dir=options.cache_dir, configs=options.configs,
                    dry_run=options.dry_run, verbose=options.verbose)
            result = poller.PollByRevision(options.revision, options.flag_check)
            log.debug("Single revision run complete: "
                      "RESULTS: %s POSTED_TO_BUG: %s"
                    % (result, result['posted_to_bug']))
        else:
            if options.starttime > time():
                log.debug("Starttime %s must be earlier than the "
                          "current time %s" % (options.starttime, localtime()))
                sys.exit(1)
            elif options.endtime < options.starttime:
                log.debug("Endtime %s must be later than the starttime %s"
                        % (options.endtime, options.starttime))
                sys.exit(1)
            elif options.endtime - options.starttime > MAX_POLLING_INTERVAL:
                log.debug("Too large of a time interval between start and "
                          "end times, please try a smaller polling interval")
                sys.exit(1)
            else:
                poller = SchedulerDBPoller(
                            branch=options.branch,
                            cache_dir=options.cache_dir,
                            configs=options.configs,
                            dry_run=options.dry_run, verbose=options.verbose,
                            messages=options.messages)
                incomplete = poller.PollByTimeRange(options.starttime,
                                                    options.endtime)
                if options.verbose:
                    log.debug("Time range run complete: INCOMPLETE %s"
                            % incomplete)
    except error.LockHeld:
        print "There is an instance of SchedulerDbPoller running already."
        print "If you're sure that it isn't running, delete %s and try again."\
                % (LOCK_FILE_PATH)
        sys.exit(1)
    finally:
        if lock_file:
            lock_file.release()

