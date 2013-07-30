import re
import logging
import os
import requests
import json

from autoland.errors import ReviewException, ApprovalException, \
    InvalidAttachment
from autoland.config import config
from .users import LDAP

log = logging.getLogger(__name__)
# TODO: celery eats logs?


class Bugzilla(object):

    def __init__(self):
        self.api_url = config["bz_api_url"]
        self.attachment_url = config["bz_attachment_url"]
        self.username = config["bz_username"]
        self.password = config["bz_password"]
        self.webui_url = config["bz_webui_url"]
        self.webui_login = config["bz_webui_login"]
        self.webui_password = config["bz_webui_password"]
        # self.ca_bundle = config["bz_ca_bundle"]
        # FIXME
        self.ca_bundle = False
        self.ldap = LDAP(
            host=config["ldap_host"], port=int(config["ldap_port"]),
            bind_dn=config["ldap_bind_dn"], password=config["ldap_password"])

    def request_json(self, url, method="GET", data=None, headers=None,
                     params=None):
        if data:
            data = json.dumps(data)
        if not headers:
            headers = {'Accept': 'application/json',
                       'Content-Type': 'application/json'}
        try:
            r = requests.request(method, url, headers=headers, data=data,
                                 params=params, verify=self.ca_bundle)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            log.error('Exception trying to access "%s"', url)
            raise
        except ValueError:
            log.error('Cannot decode:\n%s\n', r.content)
            raise

    def request(self, path, data=None, method="GET"):
        """
        Request a page through the bugzilla api.
        """
        url = self.api_url + path
        params = None
        if self.username and self.password:
            params = {"username": self.username,
                      "password": self.password}
        return self.request_json(url, method=method, data=data, params=params)

    def get_bug(self, bug_id):
        """
        Get the bug data from the BzAPI.
        """
        return self.request('bug/%s' % bug_id)

    def download_patch(self, patch_id, path='.', overwrite_patch=True):
        """
        Get a patch file from the bugzilla api. Uses the attachment url setting
        from the config file. The patch file is named {bug_id}.patch .
        If overwrite_patch is True, the patch will be overwritten if it exists,
        otherwise it will not be updated, and the path will be returned.
        """
        patch_file = '%s/%s.patch' % (path, patch_id)
        if os.path.exists(patch_file) and not overwrite_patch:
            return os.path.abspath(patch_file)
        url = self.attachment_url + str(patch_id)
        if not os.path.exists(path):
            os.makedirs(path)
        try:
            r = requests.get(url, verify=self.ca_bundle)
            r.raise_for_status()
            attachment = r.content
        except requests.RequestException:
            log.error('Exception trying to access "%s"', url, exc_info=True)
            raise
        if 'The attachment id %s is invalid' % patch_id in attachment:
            raise InvalidAttachment("Invalid attachment %s" % patch_id)
        with open(patch_file, 'wb') as f_out:
            f_out.write(attachment)
        return os.path.abspath(patch_file)

    def get_user_info(self, email):
        """
        Given a user's email address, return a dict with name and email.
        """
        user_json = self.request('user/%s' % email)
        if 'real_name' not in user_json:
            return None
        user = {}
        # drop any [:name] off the real_name
        user['name'] = re.split(r'\s*\[', user_json['real_name'], 1)[0]
        user['email'] = user_json.get('email', email)
        return user

    @staticmethod
    def bugs_from_comments(comments):
        """
        Finds things that look like bugs in comments and
        returns as a list of bug numbers.

        Supported formats:
            Bug NNN
            Bugs NNN, NNN
            bNNN
        """
        retval = []
        matches = re.search(r'\bb(?:ug(?:s)?)?\s*((?:\d+[, ]*)+)\b',
                            comments, re.I)
        if matches:
            for match in re.findall(r'\d+', matches.group(1)):
                retval.append(int(match))
        return retval

    def notify_bug(self, bug_id, message):
        self.request(path='bug/%s/comment' % bug_id,
                     data={'text': message, 'is_private': False},
                     method='POST')
        log.debug('Added comment to bug %s', bug_id)

    def has_comment(self, bug_id, comment):
        """
        Checks to see if the specified bug already has the comment text posted.
        """
        page = self.request('bug/%s/comment' % bug_id)
        for c in page.get('comments', []):
            if c['text'] == comment:
                return True
        return False

    def get_wating_auoland_bugs(self):
        """
        Polls the Bugzilla WebService API for any flagged autoland bugs.
        """
        params = {"method": "TryAutoLand.getBugs",
                  "Bugzilla_login": self.webui_login,
                  "Bugzilla_password": self.webui_password}
        try:
            # FIXME: do eet
            data = self.request_json(self.webui_url, params=params)
            #data = [{"bug_id": 872605, "branches": "try",
                     #"try_syntax": "-b do -p macosx64 -u none -t none",
                     #"attachments": [{"id": 766478, "who": "rail@mozilla.com",
                                      #"status": "waiting",
                                      #"status_when": "2013-06-10 18:22:52"}]}]
        except (ValueError, requests.RequestException):
            log.warning("Cannot retrieve bug list", exc_info=True)
            return []

        if data.get("error"):
            log.warning("autoland error: %s", data["error"])
            return []
        data = data.get("result", [])
        if data:
            # make status_when global
            # TODO: file a bug to move it to bug level
            for bug in data:
                bug["status_when"] = bug["attachments"][0]["status_when"]

        return data

    def update_attachment(self, params):
        """
        Posts the update to the Bugzilla WebService API.
        """
        params['Bugzilla_login'] = self.webui_login
        params['Bugzilla_password'] = self.webui_password
        data = {"method": "TryAutoLand.update", "version": 1.1,
                "params": params}
        return self.request_json(self.webui_url, method="POST", data=data)

    def update_autoland_status(self, status, patch_ids):
        for patch_id in patch_ids:
            params = {
                "action": "status",
                "status": status,
                "attach_id": patch_id
            }
            self.update_attachment(params)

    def remove_from_autoland_queue(self, patch_ids):
        for patch_id in patch_ids:
            params = {
                "action": "remove",
                "attach_id": patch_id
            }
            self.update_attachment(params)

    def get_reviews(self, attachment):
        """
        Takes attachment JSON, returns a list of reviews.
        Each review (in the list) is a dictionary containing:
            - Review type (review, superreview, ui-review)
            - Reviewer
            - Review Result (+, -, ?)
        """
        reviews = []
        for flag in attachment.get('flags', []):
            if flag.get('name') in ('review', 'superreview', 'ui-review'):
                review = {
                    'type': flag['name'],
                    'reviewer': self.get_user_info(flag['setter']['name']),
                    'result': flag['status']
                }
                reviews.append(review)
        return reviews

    def get_approvals(self, attachment):
        """
        Takes attachment JSON, returns a list of approvals.
        Each approval (in the list) is a dictionary containing:
            - Approval type
            - Approver
            - Approval Result (+, -, ?)
        """
        approvals = []
        for flag in attachment.get('flags', []):
            a_type = flag.get('name')
            if not a_type:
                continue
            if a_type.startswith("approval-"):
                approval = {
                    'type': a_type.replace("approval-", ""),
                    'approver': self.get_user_info(flag['setter']['name']),
                    'result': flag['status']
                }
                approvals.append(approval)
        return approvals

    def check_patch_approvals(self, patch, branch, perms):
        p_id = patch['id']
        if not patch.get('approvals'):
            raise ApprovalException(ApprovalException.INVALID,
                                    "No approvals")
        approved = False
        for a in patch['approvals']:
            if a['type'].strip().lower() != branch:
                continue
            if a['result'] == '+':
                # Found an approval, but keep on looking in case there is
                # a failed or pending approval.
                if self.ldap.in_ldap_group(a['approver']['email'], perms):
                    log.info("PERM: PATCH %s: Approver %s has valid %s "
                             "permissions for branch %s", p_id,
                             a['approver']['email'], perms, branch)
                    approved = True
                else:
                    reason = "PERM: PATCH %s: Approver %s has invalid %s " \
                             "permissions for branch %s" \
                             % (p_id, a['approver']['email'], perms, branch)
                    log.warning(reason)
                    # TODO: refactor logging, use something like
                    # autoland.perm_log.warnin(msg)
                    raise ApprovalException(ApprovalException.INVALID, reason)
            elif a['result'] == '?':
                raise ApprovalException(ApprovalException.PENDING, "Pending")
            else:
                # non-approval
                reason = "PERM: PATCH %s: Approval failed for branch %s." % \
                    (p_id, branch)
                log.warning(reason)
                raise ApprovalException(ApprovalException.FAIL, reason)
        if not approved:
            raise ApprovalException(ApprovalException.FAIL,
                                    "Cannot find proper approvals")

    def check_patch_reviews(self, patch, perms):
        p_id = patch['id']
        if not patch.get('reviews'):
            raise ReviewException(ReviewException.INVALID, "No reviews")
        for rev in patch['reviews']:
            if rev['result'] == '+':
                # Found a passed review, but keep on looking in case there is
                # a failed or pending review.
                if self.ldap.in_ldap_group(rev['reviewer']['email'], perms):
                    log.info("PERM: PATCH %s: Reviewer %s has valid %s "
                             "permissions", p_id, rev['reviewer']['email'],
                             perms)
                else:
                    reason = "PERM: PATCH %s: Reviewer %s does not have %s " \
                             "permissions" % (p_id, rev['reviewer']['email'],
                                              perms)

                    log.warn(reason)
                    raise ReviewException(ReviewException.INVALID, reason)
            elif rev['result'] == '?':
                reason = "PERM: PATCH %s: still pending review" % p_id
                raise ReviewException(ReviewException.PENDING, reason)
            else:
                # non-review
                reason = "PERM: PATCH %s: No passing review" % p_id
                log.info(reason)
                raise ReviewException(ReviewException.FAIL, reason)

    # TODO: should be Bug.get_patches(self, patch_ids)
    def get_patches(self, bug_id, patch_ids):
        """
        If patch_ids specified, only fetch the information on those specific
        patches from the bug.
        If patch_ids not specified, fetch the information on all patches from
        the bug.

        The returned patchset will return ALL patches, reviews, and approvals.

        Return value is of the JSON structure:
            [
                { 'id' : 54321,
                'author' : { 'name' : 'Name',
                            'email' : 'me@email.com' },
                'reviews' : [
                        { 'reviewer' : 'email',
                        'type' : 'superreview',
                        'result' : '+'
                        },
                        { ... }
                    ],
                'approvals' : [
                        { 'approver' : 'email',
                        'type' : 'mozilla-beta',
                        'result' : '+'
                        },
                        { ... }
                    ]
                },
                { ... }
            ]
        """

        bug_data = self.get_bug(bug_id)
        if 'attachments' not in bug_data:
            return []

        patchset = []
        patches = [p for p in bug_data['attachments']
                   if p['id'] in patch_ids and p['is_patch']
                   and not p['is_obsolete']]
        if len(patches) != len(patch_ids):
            log.error('Autoland failure. Not all patch_ids could '
                      'be picked up from bug.')
            return []

        for p in patches:
            # TODO: skip if self.get_user_info is None?
            patchset.append({
                "id": p["id"],
                "author": self.get_user_info(p["attacher"]["name"]),
                "approvals": self.get_approvals(p),
                "reviews": self.get_reviews(p)
            })
        if not patchset:
            log.error('Autoland failure. Not all patch_ids could '
                      'be picked up from bug.')

        return patchset

    def post_comment(self, comment, bug_id):
        """
        Post a comment that isn't in the comments db.
        Add it if posting fails.
        """
        self.notify_bug(bug_id=bug_id, message=comment)
        log.info('Posted comment: "%s" to %s', comment, bug_id)

    def post_failure(self, comment, bug_id):
        self.post_comment("Autoland Failure:\n\n%s" % comment, bug_id)

    def post_warning(self, comment, bug_id):
        self.post_comment("Autoland Warning:\n\n%s" % comment, bug_id)


bz = Bugzilla()
