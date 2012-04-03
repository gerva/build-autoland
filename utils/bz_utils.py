import site
site.addsitedir('vendor')
site.addsitedir('vendor/lib/python')

import urllib2, re
import logging
import os, time, datetime
from utils.common import HTTP_EXCEPTIONS
try:
    import simplejson as json
except ImportError:
    import json

log = logging.getLogger(__name__)

class bz_util(object):
    def __init__(self, api_url, attachment_url=None,
            username=None, password=None,
            webui_url=None,   # Not all tools use this
            webui_login=None, webui_password=None):
        self.api_url = api_url
        self.attachment_url = attachment_url
        self.username = username
        self.password = password
        self.webui_url = webui_url
        self.webui_login = webui_login
        self.webui_password = webui_password

# Catch these exceptions:
# bug doesn't exist
# bug can't be accessed

    def request(self, path, data=None, method=None):
        """
        Request a page through the bugzilla api.
        """
        url = self.api_url + path
        if data:
            data = json.dumps(data)

        if self.username and self.password:
            if re.search('\?', path):
                url += '&'
            else:
                url += '?'
            url += 'username=%s&password=%s' % (self.username, self.password)

        req = urllib2.Request(url, data, {'Accept': 'application/json',
                'Content-Type': 'application/json'})
        if method:
            req.get_method = lambda: method
        try:
            result = urllib2.urlopen(req)
            data = result.read()
            return json.loads(data)
        except (json.JSONDecodeError,) + HTTP_EXCEPTIONS, err:
            log.error('REQUEST ERROR: %s: %s' % (err, url))
            raise

    def put_request(self, path, data, retries, interval):
        """
        Perform a PUT request, raise 'PutError' if can't complete.
        """
        result = 0
        for i in range(retries):
            log.debug('Put attempt %s of %s' % (i + 1, retries))
            # PUT the changes
            try:
                result = self.request(path=path, data=data, method='PUT')
                if 'ok' in result and result['ok'] == 1:
                    log.debug('Put success')
                    return result
                time.sleep(interval)
            except HTTP_EXCEPTIONS:
                if i < retries:
                    continue
                else:
                    raise
        log.debug(result)
        # XXX: This exceptions will need to be made a class member, and
        # explicitly caught.
        raise Exception('PutError')

    def get_patch(self, patch_id, path='.', create_path=False,
            overwrite_patch=False):
        """
        Get a patch file from the bugzilla api. Uses the attachment url setting
        from the config file. The patch file is named {bugid}.patch .
        If create_path is True, path will be created if it does not exist.
        If overwrite_patch is True, the patch will be overwritten if it exists,
        otherwise it will not be updated, and the path will be returned.
        """
        patch_file = '%s/%s.patch' % (path, str(patch_id))
        url = self.attachment_url + str(patch_id)
        if not os.access(path, os.F_OK):
            os.makedirs(path)
        if os.access(patch_file, os.F_OK) and not overwrite_patch:
            return patch_file
        try:
            data = urllib2.urlopen(url).read()
        except HTTP_EXCEPTIONS, err:
            log.error('Error reading patch %s: %s' % (err, url))
            return None
        if re.search('The attachment id %s is invalid' % str(patch_id), data):
            return None
        with open(patch_file, 'w') as f_out:
            f_out.write(data)
        return os.path.abspath(patch_file)

    def get_user_info(self, email):
        """
        Given a user's email address, return a dict with name and email.
        """
        if not email:
            return None
        data = self.request('user/%s' % (email))
        if 'real_name' not in data:
            return None
        info = {}
        # drop any [:name] off the real_name
        info['name'] = re.split('\s*\[', data['real_name'], 1)[0]
        info['email'] = data.get('email', email)
        return info

    def bugs_from_comments(self, comments):
        """
        Finds things that look like bugs in comments and
        returns as a list of bug numbers.

        Supported formats:
            Bug XXXXX
            Bugs XXXXXX, YYYYY
            bXXXXX
        """
        retval = []
        # TODO - add word boundary in front and behind the bug number
        # Add test cases for this (remove the 9000)
        matches = re.search(r'\bb(?:ug(?:s)?)?\s*((?:\d+[, ]*)+)', comments,
                re.I)
        if matches:
            for match in re.findall('\d+', matches.group(1)):
                # lower the odds of getting a false bug number from an hg cset
                if int(match) > 9000:
                    retval.append(int(match))
        return retval

    def notify_bug(self, message, bug_num, retries=5):
        result = 0
        for i in range(retries):
            log.debug('Getting bug %s', bug_num)
            try:
                # Make sure we can reach this bug
                bug = self.request('bug/%s' % (bug_num))
                # Add the comment
                self.request(path='bug/%s/comment' % (bug_num),
                        data={'text': message, 'is_private': False},
                        method='POST')
                log.debug('Added comment to bug %s' % (bug_num))
                result = 1
            except ((Exception,) + HTTP_EXCEPTIONS), err:
                log.debug('Couldn\t get bug, retry %d of %d -- %s'
                        % (i+1, retries, err))
                result = 0
                if i < retries:
                    continue
                else:
                    raise
            break
        log.debug('BUG NOTIFY RESULTS: %s' % result)
        return result

    def has_comment(self, text, bugid):
        """
        Checks to see if the specified bug already has the comment text posted.
        """
        result = 0
        try:
            page = self.request('bug/%s/comment' % (bugid))
            if not 'comments' in page:
                # error, we shouldn't be here
                pass
            for comment in page['comments']:
                if comment['text'] == text:
                    result = 1
        except HTTP_EXCEPTIONS, err:
            log.debug('HTTPError, Can\'t check comments on bug: %s' % (err))

        return result

    def has_recent_comment(self, regex, bugid, hours=4):
        """
        Returns true if there is a comment matching regex posted in the past
        number of hours specified.
        """
        try:
            page = self.request(
                    'bug/%s/comment?include_fields=creation_time,text'
                    % (bugid))
        except HTTP_EXCEPTIONS, err:
            log.debug('Couldn\'t get page: %s' % (err))
            return False
        if not page or not 'comments' in page:
            # error, we shouldn't be here
            return False
        current_time = datetime.datetime.utcnow()
        for comment in page['comments']:
            # May need to account for timezone
            creation_time = datetime.datetime.strptime(
                    comment['creation_time'], '%Y-%m-%dT%H:%M:%SZ')
            if (current_time - creation_time) < \
                    datetime.timedelta(hours=hours):
                if re.search(regex, comment['text'], re.I):
                    return True
        return False

    def get_matching_bugs(self, field, match_string, match_type='regex'):
        """
        Returns bugids whose text field matches the match_string using
        match_type.
        Eg. get_matching_bugs('whiteboard', 'Autoland', 'regex')
                will return all bugs that contain the word Autoland in
                their whiteboard.
        For list of types and more information on fields, see
        https://wiki.mozilla.org/Bugzilla:REST_API:Search
        Note that for this api, the regex need not escape [] characters.
        """
        page = self.request('bug/?%s=%s&%s_type=%s&include_fields=id,%s'
                % (field, match_string, field, match_type, field))
        if not 'bugs' in page:
            # error, we shouldn't be here
            return []
        bugs = []
        for bug in page['bugs']:
            bugs.append((bug['id'], bug[field]))
        return bugs

    def autoland_get_bugs(self, retries=5):
        """
        Polls the Bugzilla WebService API for any flagged autoland bugs.
        """
        url = self.webui_url + "?method=TryAutoLand.getBugs"
        url = url + "&Bugzilla_login=%s" % (self.webui_login)
        url = url + "&Bugzilla_password=%s" % (self.webui_password)
        req = urllib2.Request(url, None, {'Accept':'application/json',
                    'Content-Type':'application/json'})
        for i in range(retries):
            try:
                result = urllib2.urlopen(req)
                data = result.read()
                data = json.loads(data)
                if data.get('error'):
                    # an error occurred
                    log.error('TryAutoLand.getBugs(): %s: %s'
                            % (data['error'], url))
                    continue
                return data.get('result')
            except (urllib2.HTTPError, urllib2.URLError), err:
                log.error('REQUEST ERROR: %s: %s' % (err, url))
        return None

    def autoland_update_attachment(self, params, retries=5):
        """
        Posts the update to the Bugzilla WebService API.
        """
        params['Bugzilla_login'] = self.webui_login
        params['Bugzilla_password'] = self.webui_password
        post_body = json.dumps({
                "method":"TryAutoLand.update",
                "version":1.1,
                "params":params
            })
        url = self.webui_url
        req = urllib2.Request(url, post_body, {'Accept':'application/json',
                    'Content-Type':'application/json'})
        req.get_method = lambda: "POST"
        for i in range(retries):
            try:
                result = urllib2.urlopen(req)
                data = result.read()
                try:
                    data = json.loads(data)
                except ValueError:
                    log.error('TryAutoLand.update() didn\'t '
                              'return a JSON structure')
                    print "retry #%d" % (i)
                    continue

                if data.get('error'):
                    print data['error']
                    log.error('TryAutoLand.update(): %s'
                            % (data['error']))
                    print "retry #%d" % (i)
                    continue
                return data
            except (urllib2.HTTPError, urllib2.URLError), err:
                log.error('REQUEST ERROR: %s: %s' % (err, url))
        return None

