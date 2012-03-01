import time
import os, sys
import logging
import logging.handlers
import datetime
import urllib2

from utils import mq_utils, bz_utils, common
base_dir = common.get_base_dir(__file__)
import site
site.addsitedir('%s/../../lib/python' % (base_dir))

from utils.db_handler import DBHandler, PatchSet, Branch, Comment


log = logging.getLogger()
LOGFORMAT = logging.Formatter(
        '%(asctime)s\t%(module)s\t%(funcName)s\t%(message)s')
LOGFILE = os.path.join(base_dir, 'autoland_queue.log')
LOGHANDLER = logging.handlers.RotatingFileHandler(LOGFILE,
                    maxBytes=50000, backupCount=5)

config = common.get_configuration(os.path.join(base_dir, 'config.ini'))
BZ = bz_utils.bz_util(api_url=config['bz_api_url'], url=config['bz_url'],
        attachment_url=config['bz_attachment_url'],
        username=config['bz_username'], password=config['bz_password'])
MQ = mq_utils.mq_util()
DB = DBHandler(config['databases_autoland_db_url'])

if config.get('staging', False):
    import subprocess

def get_reviews(attachment):
    """
    Takes attachment JSON, returns a list of reviews.
    Each review (in the list) is a dictionary containing:
        - Review type (review, superreview, ui-review)
        - Reviewer
        - Review Result (+, -, ?)
    """
    reviews = []
    if not 'flags' in attachment:
        return reviews
    for flag in attachment['flags']:
        for review_type in ('review', 'superreview', 'ui-review'):
            if flag.get('name') == review_type:
                reviews.append({'type':review_type,
                                'reviewer':flag['setter']['name'],
                                'result':flag['status']})
                break
    return reviews

def get_patchset(bug_id, try_run, user_patches=None, review_comment=True):
    """
    If user_patches specified, only fetch the information on those specific
    patches from the bug.
    If user_patches not specified, fetch the information on all patches from
    the bug.

    Try runs will contain all non-obsolete patches posted on the bug, no
    matter the state of the reviews. This means that it will take even
    patches that are R- but non-obsolete.

    Pushes to branch will contain all patches that are posted to the bug
    which have R+ on any R that is set. If there are any non-obsolete
    bugs that have R-, the push will fail since the bug may not be
    complete.

    The review_comment parameter defaults to True, and is used to specify
    if a comment should be posted on review failures on not. This has a
    somewhat specific use case:
        When checking if a flagged job should be picked up and put into the
        queue, no comment should be posted if there are missing/bad reviews.

    Return value is of the JSON structure:
        [
            { 'id' : 54321,
              'author' : { 'name' : 'Name',
                           'email' : 'me@email.com' },
              'reviews' : [
                    { 'reviewer' : { 'name' : 'Rev. Name',
                                     'email' : 'rev@email.com' },
                      'type' : 'superreview',
                      'result' : '+'
                    },
                    { ... }
                ]
            },
            { ... }
        ]
    """
    patchset = []   # hold the final patchset information
    reviews = []    # hold the review information corresponding to each patch

    # grab the bug data
    bug_data = BZ.request('bug/%s' % (bug_id))
    if 'attachments' not in bug_data:
        return None     # bad bug id, or no attachments

    if user_patches:
        # user-specified patches, need to pull them in that set order
        user_patches = list(user_patches)    # take a local copy, passed byref
        for user_patch in tuple(user_patches):
            for attachment in bug_data['attachments']:
                if attachment['id'] != user_patch or \
                        not attachment['is_patch'] or \
                        attachment['is_obsolete']:
                    continue
                patch = { 'id' : user_patch,
                          'author' :
                              BZ.get_user_info(attachment['attacher']['name']),
                          'reviews' : [] }
                reviews.append(get_reviews(attachment))
                patchset.append(patch)
                # remove the patch from user_patches to check all listed
                # patches were pulled
                user_patches.remove(patch['id'])
        if len(user_patches) != 0:
            # not all requested patches could be picked up
            # XXX TODO - should we still push what patches _did get picked up?
            log.debug('Autoland failure. Not all user_patches could '
                      'be picked up from bug.')
            post_comment(('Autoland Failure\nSpecified patches %s '
                          'do not exist, or are not posted to this bug.'
                          % (user_patches)), bug_id)
            return None
    else:
        # no user-specified patches, grab them in the order they were posted.
        for attachment in bug_data['attachments']:
            if not attachment['is_patch'] or attachment['is_obsolete']:
                # not a valid patch to be pulled
                continue
            patch = { 'id' : attachment['id'],
                      'author' :
                          BZ.get_user_info(attachment['attacher']['name']),
                      'reviews' : [] }
            reviews.append(get_reviews(attachment))
            patchset.append(patch)

    # check the reviews, based on try, etc, etc.
    for patch, revs in zip(patchset, reviews):
        if try_run:
            # on a try run, take all non-obsolete patches
            patch['reviews'] = revs
            continue

        # this is a branch push
        if not revs:
            if review_comment:
                post_comment('Autoland Failure\nPatch %s requires review+ '
                             'to push to branch.' % (patch['id']), bug_id)
                return None
            for rev in revs:
                if rev['result'] != '+':    # Bad review, fail
                    if review_comment:
                        post_comment('Autoland Failure\nPatch %s has a '
                                     'non-passing review. Requires review+ '
                                     'to push to branch.'
                                     % (patch['id']), bug_id)
                    return None
                rev['reviewer'] = BZ.get_user_info(rev['reviewer'])
            patch['reviews'] = revs

    if len(patchset) == 0:
        post_comment('Autoland Failure\n There are no patches to run.', bug_id)
    return patchset

def bz_search_handler():
    """
    Query Bugzilla WebService API for Autoland flagged bugs.
    For the moment, only supports push to try,
    and then to branch. It cannot push directly to branch.
    """
    bugs = []
    try:
        bugs = BZ.autoland_get_bugs()
    except (urllib2.HTTPError, urllib2.URLError), err:
        log.error('Error while querying WebService API: %s' % (err))
        return

    for bug in bugs:
        bug_id = bug.get('bug_id')

        # Grab the branches as a list, do a bit of cleaning
        branches = bug.get('branches', 'try').split(',')
        branches = [x.strip() for x in branches]
        branches = [y for y in branches if y != '']

        for branch in tuple(branches):
            # clean out any invalid branch names
            # job will still land to any correct branches
            b = DB.BranchQuery(Branch(name=branch))
            if b == None:
                branches.remove(branch)
                log.info('Branch %s does not exist.' % (branch))
            elif b.status != 'enabled':
                branches.remove(branch)
                log.info('Branch %s is not enabled.' % (branch))
        if not branches:
            log.info('Bug %d had no correct branches flagged' % (bug_id))
            continue

        # patches are taken from the 'attachments' element of bug
        # the only patches that should be taken are the patches with status
        # 'waiting'
        patch_group = bug.get('attachments', None)
        # take only waiting patches
        patch_group = [x for x in patch_group if x['status'] == 'waiting']

        # XXX XXX: Should only patches with 'who' the same be pulled into the
        # single patch set? Or could be done by 'status_when'

        patch_set = PatchSet()
        # all runs will get a try_run by default for now
        patch_set.try_syntax = patch_group[0]['try_syntax']
        patch_set.bug_id = bug_id
        patch_set.branch = ','.join(branches)  # branches have been filtered out
        patch_set.patches = [x['id'] for x in patch_group]

        if DB.PatchSetQuery(patch_set) != None:
            # we already have this in the db, don't add it.
            # XXX: Need to update the bug
            for patch in bug['attachments']:
                pass
                #BZ.autoland_update_attachment(.....)
            continue
        # add try_run attribute here so that PatchSetQuery will match patchsets
        # in any stage of their lifecycle
        patch_set.try_run = 1

        patch_set.author = patch_group[0]['who']

        log.info('Inserting job: %s' % (patch_set))
        patchset_id = DB.PatchSetInsert(patch_set)

        # XXX: Update the bug
        #BZ.autoland_update_attachment(.....)


def message_handler(message):
    """
    Handles json messages received. Expected structures are as follows:
    For a JOB:
        {
            'type' : 'job',
            'bug_id' : 12345,
            'branch' : 'mozilla-central',
            'try_run' : 1,
            'patches' : [ 53432, 64512 ],
        }
    For a SUCCESS/FAILURE:
        {
            'type' : 'error',
            'action' : 'patchset.apply',
            'patchsetid' : 123,
        }
    For try run PASS/FAIL:
        {
            'type' : 'success',
            'action' : 'try.run',
            'revision' : '8dc05498d708',
        }
    """
    msg = message['payload']
    if not 'type' in msg:
        log.error('Got bad mq message: %s' % (msg))
        return
    if msg['type'] == 'job':
        if 'try_run' not in msg:
            msg['try_run'] = 1
        if 'bug_id' not in msg:
            log.error('Bug ID not specified.')
            return
        if 'branches' not in msg:
            log.error('Branches not specified.')
            return
        if 'patches' not in msg:
            log.error('Patch list not specified')
            return
        if msg['try_run'] == 0:
            # XXX: Nothing to do, don't add.
            log.error('ERROR: try_run not specified.')
            return

        if msg['branches'].lower() == ['try']:
            msg['branches'] = ['mozilla-central']
            msg['try_run'] = 1

        patch_set = PatchSet(bug_id=msg.get('bug_id'),
                      branch=msg.get('branch'),
                      try_run=msg.get('try_run'),
                      try_syntax=msg.get('try_syntax'),
                      patches=msg.get('patches')
                     )
        patchset_id = DB.PatchSetInsert(patch_set)
        log.info('Insert PatchSet ID: %s' % (patchset_id))

    comment = msg.get('comment')
    if comment:
        # Handle the posting of a comment
        bug_id = msg.get('bug_id')
        if not bug_id:
            log.error('Have comment, but no bug_id')
        else:
            post_comment(comment, bug_id)

    if msg['type'] == 'success':
        if msg['action'] == 'try.push':
            # Successful push, add corresponding revision to patchset
            patch_set = DB.PatchSetQuery(PatchSet(id=msg['patchsetid']))
            if patch_set == None:
                log.error('No corresponding patch set found for %s'
                        % (msg['patchsetid']))
                return
            patch_set = patch_set[0]
            log.debug('Got patchset back from DB: %s' % (patch_set))
            patch_set.revision = msg['revision']
            DB.PatchSetUpdate(patch_set)
            log.debug('Added revision %s to patchset %s'
                    % (patch_set.revision, patch_set.id))

        elif '.run' in msg['action']:
            # this is a result from schedulerDBpoller
            patch_set = DB.PatchSetQuery(PatchSet(revision=msg['revision']))
            if patch_set == None:
                log.error('Revision %s not found in database.'
                        % (msg['revision']))
                return
            patch_set = patch_set[0]
            # is this the try run before push to branch?
            if patch_set.try_run and msg['action'] == 'try.run' \
                    and patch_set.branch != 'try':
                # remove try_run, when it comes up in the
                # queue it will trigger push to branch(es)
                patch_set.try_run = 0
                patch_set.push_time = None
                log.debug('Flagging patchset %s revision %s '
                        'for push to branch(es).'
                        % (patch_set.id, patch_set.revision))
            else:
                # close it!
                BZ.remove_whiteboard_tag('\[autoland-in-queue\]',
                        patch_set.bug_id)
                DB.PatchSetDelete(patch_set)
                log.debug('Deleting patchset %s' % (patch_set.id))
                return

        elif msg['action'] == 'branch.push':
            # Guaranteed patchset EOL
            patch_set = DB.PatchSetQuery(PatchSet(id=msg['patchsetid']))[0]
            BZ.remove_whiteboard_tag('\[autoland-in-queue\]', patch_set.bug_id)
            DB.PatchSetDelete(patch_set)
            log.debug('Successful push to branch of patchset %s.'
                    % (patch_set.id))
    elif msg['type'] == 'timed out':
        patch_set = None
        if msg['action'] == 'try.run':
            patch_set = DB.PatchSetQuery(PatchSet(revision=msg['revision']))
            if patch_set == None:
                log.error('No corresponding patchset found '
                        'for timed out revision %s' % msg['revision'])
                return
            patch_set = patch_set[0]
        if patch_set:
            # remove it from the queue, timeout should have been comented to bug
            # XXX: (shall we confirm that here with bz_utils.has_comment?)
            BZ.remove_whiteboard_tag('\[autoland-in-queue\]', patch_set.bug_id)
            DB.PatchSetDelete(patch_set)
            log.debug('Received time out on %s, deleting patchset %s'
                    % (msg['action'], patch_set.id))
    elif msg['type'] == 'error' or msg['type'] == 'failure':
        patch_set = None
        if msg['action'] == 'try.run' or msg['action'] == 'branch.run':
            patch_set = DB.PatchSetQuery(PatchSet(revision=msg['revision']))
            if patch_set == None:
                log.error('No corresponding patchset found for revision %s'
                        % (msg['revision']))
                return
            patch_set = patch_set[0]
        elif msg['action'] == 'patchset.apply':
            patch_set = DB.PatchSetQuery(PatchSet(id=msg['patchsetid']))
            if patch_set == None:
                log.error('No corresponding patchset found for revision %s'
                        % msg['revision'])
                return
            patch_set = patch_set[0]

        if patch_set:
            # remove it from the queue, error should have been comented to bug
            # XXX: (shall we confirm that here with bz_utils.has_coment?)
            BZ.remove_whiteboard_tag('\[autoland-in-queue\]', patch_set.bug_id)
            DB.PatchSetDelete(patch_set)
            log.debug('Received error on %s, deleting patchset %s'
                    % (msg['action'], patch_set.id))

def handle_patchset(patchset):
    """
    Message sent to HgPusher is of the JSON structure:
        {
          'job_type' : 'patchset',
          'bug_id' : 12345,
          'branch' : 'mozilla-central',
          'push_url' : 'ssh://hg.mozilla.org/try',
          'branch_url' : 'ssh://hg.mozilla.org/mozilla-central',
          'try_run' : 1,
          'try_syntax': '-p linux -u mochitests',
          'patchsetid' : 42L,
          'patches' :
                [
                    { 'id' : 54321,
                      'author' : { 'name' : 'Name',
                                   'email' : 'me@email.com' },
                      'reviews' : [
                            { 'reviewer' : { 'name' : 'Rev. Name',
                                             'email' : 'rev@email.com' },
                              'type' : 'superreview',
                              'result' : '+'
                            },
                            { ... }
                        ]
                    },
                    { ... }
                ]
        }
    """
    log.debug('Handling patchset %s from queue.' % (patchset))

    # TODO: Check the retries & creation time.

    # Check permissions & patch set again, in case it has changed
    # since the job was put on the queue.
    patches = get_patchset(patchset.bug_id, patchset.try_run,
                           user_patches=patchset.patchList())
    # get branch information so that message can contain branch_url
    branch = DB.BranchQuery(Branch(name=patchset.branch))
    if not branch:
        # error, branch non-existent
        # XXX -- Should we email or otherwise let user know?
        log.error('Could not find %s in branches table.' % (patchset.branch))
        DB.PatchSetDelete(patchset)
        return
    branch = branch[0]
    jobs = DB.BranchRunningJobsQuery(Branch(name=patchset.branch))
    log.debug("Running jobs on %s: %s" % (patchset.branch, jobs))
    b = DB.BranchQuery(Branch(name='try'))[0]
    log.debug("Threshold for %s: %s" % (patchset.branch, b.threshold))
    if jobs < b.threshold:
        message = { 'job_type':'patchset', 'bug_id':patchset.bug_id,
                'branch_url':branch.repo_url,
                'branch':patchset.branch, 'try_run':patchset.try_run,
                'try_syntax':patchset.try_syntax,
                'patchsetid':patchset.id, 'patches':patches }
        if patchset.try_run == 1:
            tb = DB.BranchQuery(Branch(name='try'))
            if tb: tb = tb[0]
            else: return
        log.info("SENDING MESSAGE: %s" % (message))
        MQ.send_message(message, routing_key='hgpusher')
        patchset.push_time = datetime.datetime.utcnow()
        DB.PatchSetUpdate(patchset)
    else:
        log.info("Too many jobs running right now, will have to wait.")
        patchset.retries += 1
        DB.PatchSetUpdate(patchset)

def handle_comments():
    """
    Queries the Autoland DB for any outstanding comments to be posted.
    Gets the five oldest comments and tries to post them on the corresponding
    bug. In case of failure, the comments attempt count is updated, to be
    picked up again later.
    If we have attempted 5 times, get rid of the comment and log it.
    """
    comments = DB.CommentGetNext(limit=5)   # Get up to 5 comments
    for comment in comments:
        # Note that notify_bug makes multiple retries
        success = BZ.notify_bug(comment.comment, comment.bug)
        if success:
            # Posted. Get rid of it.
            DB.CommentDelete(comment)
        elif comment.attempts == 5:
            # 5 attempts have been made, drop this comment as it is
            # probably not going anywhere.
            # XXX: Perhaps this should be written to a file.
            log.error("Could not post comment to bug %s. Dropping comment: %s"
                    % (comment.bug, comment.comment))
            DB.CommentDelete(comment.id)
        else:
            comment.attempts += 1
            DB.CommentUpdate(comment)

def post_comment(comment, bug_id):
    """
    Post a comment that isn't in the comments db.
    Add it if posting fails.
    """
    success = BZ.notify_bug(comment, bug_id)
    if success:
        log.info('Posted comment: "%s" to %s' % (comment, bug_id))
    else:
        log.info('Could not post comment to bug %s. Adding to comments table'
                % (bug_id))
        cmnt = Comment(comment=comment, bug=bug_id)
        DB.CommentInsert(cmnt)

def main():
    MQ.set_host(config['mq_host'])
    MQ.set_exchange(config['mq_exchange'])
    MQ.connect()

    log.setLevel(logging.DEBUG)
    LOGHANDLER.setFormatter(LOGFORMAT)
    log.addHandler(LOGHANDLER)

    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg == '--purge-queue':
                # purge the autoland queue
                MQ.purge_queue(config['mq_autoland_queue'], prompt=True)
                exit(0)

    while True:
        # search bugzilla for any relevant bugs
        bz_search_handler()
        next_poll = time.time() + int(config['bz_poll_frequency'])

        if config.get('staging', False):
            # if this is a staging instance, launch schedulerDbPoller in order
            # to poll by revision. This will allow for posting back to
            # landfill.
            for revision in DB.PatchSetGetRevs():
                cmd = ['bash', os.path.join(base_dir,
                                    'run_schedulerDbPoller_staging')]
                cmd.append(revision)
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
                (out, err) = proc.communicate()
                log.info('schedulerDbPoller Returned: %d' % (proc.returncode))
                log.info('stdout: %s' % (out))
                log.info('stderr: %s' % (err))

        while time.time() < next_poll:
            patchset = DB.PatchSetGetNext()
            if patchset != None:
                handle_patchset(patchset)

            # take care of any comments that couldn't previously be posted
            handle_comments()

            # loop while we've got incoming messages
            while MQ.get_message(config['mq_autoland_queue'],
                    message_handler, routing_key='db'):
                continue

if __name__ == '__main__':
    main()

