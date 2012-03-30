import site
site.addsitedir('vendor')
site.addsitedir('vendor/lib/python')

import ldap
import urllib2
import logging

import common
base_dir = common.get_base_dir(__file__)
import site
site.addsitedir('%s/../../lib/python' % (base_dir))
from util.retry import retriable

log = logging.getLogger(__name__)

class ldap_util():
    def __init__(self, host, port, branch_api='', bind_dn='', password=''):
        self.host = host
        self.port = port
        self.bind_dn = bind_dn
        self.password = password
        self.connection = self._connect()
        self.branch_api = branch_api

    def _connect(self):
        return ldap.initialize('ldap://%s:%s' % (self.host, self.port))

    def _bind(self):
        self.connection.simple_bind(self.bind_dn, self.password)
        self.connection.result(timeout=10) # get rid of bind result

    #@retriable(cleanup=ldap_util._connect)
    def search(self, bind, filterstr, attrlist=None,
            scope=ldap.SCOPE_SUBTREE):
        """
        A wrapper for ldap.search() to allow for retry on lost connection.
        Handles all connecting and binding prior to search and retries.
        Returns True on successful search and false otherwise.
        Results need to be grabbed using connection.result()

        Note that failures will be common, since connection closes at a certain
        point of inactivity, and needs to be re-established. Expect 2 attempts.
        """
        result = None
        for i in range(5):
            try:
                self._bind()
                self.connection.search(bind, scope,
                        filterstr=filterstr, attrlist=attrlist)
                result = self.connection.result(timeout=10)
                log.info('Success')
            except:
                self._connect()

        return result

    def get_group_members(self, group):
        """
        Return a list of all members of the groups searched for.
        """
        members = []
        result = self.search('ou=groups,dc=mozilla',
                filterstr='cn=%s' % (group))
        if not result:
            return []
        for group in result[1]:
            # get the union of all members in the searched groups
            members = list(set(members) | set(group[1]['memberUid']))
        return members

    def is_member_of_group(self, mail, group):
        """
        Check if a member is in a group, or set of groups. Supports LDAP search
        strings eg. 'scm_level_*' will find members of groups of 'scm_level_1',
        'scm_level_2', and 'scm_level_3'.
        """
        members = self.get_group_members(group)
        return mail in members

    def get_member(self, filter_, attrlist=None):
        """
        Search for member in o=com,dc=mozilla, using the given filter.
        The filter can be a properly formed LDAP query.
            see http://tools.ietf.org/html/rfc4515.html for more info.
        Some useful filers are:
            'bugzillaEmail=example@mail.com'
            'mail=example@mozilla.com'
            'sn=Surname'
            'cn=Common Name'
        attrlist can be specified as a list of attributes that should be
        returned.
        Some useful attributes are:
            bugzillaEmail
            mail
            sn
            cn
            uid
            sshPublicKey
        """
        result = self.search('o=com,dc=mozilla', filter_, attrlist)
        if not result:
            return []
        return result[1]

    @retriable()
    def get_branch_permissions(self, branch):
        """
        Queries the branch permissions api for the
        permission level on that branch.
            eg. scm_level_3
        """
        req = urllib2.Request(self.branch_api+branch)
        result = urllib2.urlopen(req)
        perms = result.read()
        perms = perms.rstrip()
        if 'is not an hg repository' in perms:
            return None
        if 'Need a repository' in perms or \
               'A problem occurred' in perms:
            log.error('An error has occurred with branch permissions api:\n'
                      '\turl: %s\n\tresponse: %s' % (url, perms))
            raise Exception
        log.info('Required permissions for %s: %s' % (branch, perms))
        return perms

    def get_bz_email(self, email):
        member = self.get_member('bugzillaEmail=%s' % (email), ['mail'])
        try:
            bz_email = member[0][1]['mail'][0]
        except (IndexError, KeyError):
            bz_email = None
        return bz_email

