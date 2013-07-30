from __future__ import absolute_import
import ldap
import logging


log = logging.getLogger(__name__)


class LDAP():
    def __init__(self, host, port, bind_dn='', password=''):
        self.host = host
        self.port = port
        self.bind_dn = bind_dn
        self.password = password
        self.conn = ldap.initialize('ldap://%s:%s' % (self.host, self.port))

    # TODO: make it retriable
    def search(self, bind, filterstr, attrlist=None, scope=ldap.SCOPE_SUBTREE):
        """
        A wrapper for ldap.search() to allow for retry on lost connection.
        Handles all connecting and binding prior to search and retries.
        Returns True on successful search and false otherwise.
        Results need to be grabbed using connection.result()

        Note that failures will be common, since connection closes at a certain
        point of inactivity, and needs to be re-established. Expect 2 attempts.
        """
        log.debug('Search for: %s; filter: %s', bind, filterstr)
        self.conn.simple_bind_s(self.bind_dn, self.password)
        self.conn.search(bind, scope, filterstr=filterstr, attrlist=attrlist)
        return self.conn.result(timeout=10)

    def get_group_members(self, group):
        """
        Return a list of all members of the groups searched for.
        """
        members = []
        result = self.search('ou=groups,dc=mozilla',
                             filterstr='cn=%s' % group)
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

    def get_member(self, filterstr, attrlist=None):
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
        result = self.search('o=com,dc=mozilla', filterstr, attrlist)
        if not result:
            return []
        # Why not 0?
        return result[1]

    def get_bz_email(self, email):
        member = self.get_member(filterstr='bugzillaEmail=%s' % email,
                                 attrlist=['mail'])
        try:
            bz_email = member[0][1]['mail'][0]
        except (IndexError, KeyError, TypeError):
            bz_email = None
        return bz_email

    def in_ldap_group(self, email, group):
        """
        Checks ldap if either email or the bz_email are a member of the group.
        """
        if self.is_member_of_group(email, group):
            return True
        bz_email = self.get_bz_email(email)
        if bz_email and self.is_member_of_group(bz_email, group):
            return True
        return False
