from __future__ import absolute_import
import logging
from sqlalchemy import Column, Integer, String, Boolean
import requests
import re
import autoland.errors
from autoland.config import config
from .db import db, Base

log = logging.getLogger(__name__)


class Branch(Base):
    __tablename__ = "branch"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)
    pull_repo = Column(String)
    push_repo = Column(String, unique=True)
    # TODO: tbpl_name to be used in the emails
    tbpl_name = Column(String, unique=True)
    enabled = Column(Boolean)
    approval_required = Column(Boolean)
    review_required = Column(Boolean)
    add_try_commit = Column(Boolean)
    use_tree_status = Column(Boolean)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "%s<%s, e:%s, r: %s, a: %s>" % (
            self.__class__.__name__, self.name, self.enabled,
            self.review_required, self.approval_required)

    @classmethod
    def get_by_id(cls, branch_id):
        return db.session.query(cls).get(branch_id)

    @classmethod
    def get_by_name(cls, name):
        return db.session.query(cls).filter_by(name=name).one()

    @classmethod
    def parse_branches(cls, line):
        if not line:
            return []
        branches = re.split(r"[\s,]", line)
        branches = filter(None, branches)
        # remove duplicates
        branches = list(set(branches))
        branches.sort()
        return branches

    @classmethod
    def active_branches(cls):
        q = db.session.query(cls.name).filter_by(enabled=True)
        return [r[0] for r in q.all()]

    @classmethod
    def is_active(cls, branch):
        return branch in cls.active_branches()

    @property
    def scm_level(self):
        """
        Queries the branch permissions api for the
        permission level on that branch.
            eg. scm_level_3
        """
        # TODO: better error handling
        # TODO: have to use releases/mozilla-release
        url = "%s?repo=%s" % (config["branch_api"], self.name)
        res = requests.get(url).content.strip()
        if 'is not an hg repository' in res:
            raise autoland.errors.BranchDoesNotExist
        if 'Need a repository' in res or 'A problem occurred' in res:
            log.error('An error has occurred with branch permissions api:\n'
                      '\turl: %s\n\tresponse: %s', url, res)
            raise Exception('Corrupted repo? %s' % res)
        log.debug('Required permissions for %s: %s', self.name, res)
        return res

    def get_tree_status(self):
        if not self.use_tree_status:
            return "open"
        # TODO: check SSL
        # TODO: handle exceptions
        url = "%s%s?format=json" % (config["tree_status_api"], self.name)
        log.debug("Fetching %s", url)
        return requests.get(url, verify=False).json()["status"]

    def tree_closed(self):
        return self.get_tree_status() == "closed"

    def patch_has_proper_reviews(self, bz, patch):
        if not patch.reviews:
            return False
        for review in patch.reviews:
            if review["result"] != "+":
                return False
            else:
                if not bz.ldap.in_ldap_group(review['reviewer']['email'],
                                             self.scm_leve):
                    return False
        return True

    def patch_has_proper_approvals(self, bz, patch):
        if not patch.approvals:
            return False
        approved = False
        branch_approvals = [a for a in patch.approvals
                            if a["type"] == self.name and
                            a["result"] == "+"]
        for a in branch_approvals:
            # TODO: do we really need to check the following?
            # a+ is something different than scm_level
            # should be something like
            # if user in approval group for branch X (from bz or ldap)
            if bz.ldap.in_ldap_group(a['approver']['email'], self.scm_level):
                approved = True
            else:
                return False
        return approved

    def patch_applicable(self, bz, patch):
        # short cut, approve any patch for branches like try
        if not self.review_required:
            log.info("in patches_applicable 1")
            return True
        # make sure that the patch is reviewed
        if not self.patch_has_proper_reviews(bz, patch):
            log.info("in patches_applicable 2")
            return False
        # check if the branch requires approvals
        if self.approval_required and not \
           self.patch_has_proper_approvals(bz, patch):
            log.info("in patches_applicable 3")
            return False
        log.info("in patches_applicable 4")
        return True

    def patches_applicable(self, bz, patches):
        return all(self.patch_applicable(bz, p) for p in patches)
