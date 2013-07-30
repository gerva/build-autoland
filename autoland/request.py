from __future__ import absolute_import
from datetime import datetime
from collections import namedtuple
from sqlalchemy import Column, Integer, String, DateTime
from .db import Base, db, str_to_datetime
from .branch import Branch
from .patch import Patch
from .bugzilla import bz


class AutolandRequest(Base):
    __tablename__ = "autoland_request"
    id = Column(Integer, primary_key=True)
    bug_id = Column(String)
    branches = Column(String)
    patches = Column(String)
    status_when = Column(DateTime)
    task_id = Column(String)
    try_syntax = Column(String)
    status = Column(String)

    def __init__(self, bug_id, branches, patches, status_when, task_id=None,
                 status=None, try_syntax=None):
        self.bug_id = bug_id
        self.branches = branches
        if not isinstance(self.branches, basestring):
            self.branches = ','.join(self.branches)
        self.patches = patches
        if not isinstance(self.patches, basestring):
            self.patches = ','.join(map(str, self.patches))
        self.task_id = task_id
        self.try_syntax = try_syntax
        if isinstance(status_when, datetime):
            self.status_when = status_when
        else:
            self.status_when = str_to_datetime(status_when)
        self.status = status

    def save(self):
        if self not in db.session:
            db.session.add(self)
        db.session.commit()

    @classmethod
    def get_by_id(cls, task_id):
        return db.session.query(cls).filter_by(id=task_id).one()

    @classmethod
    def processed(cls, bug_id, status_when):
        status_when = str_to_datetime(status_when)
        cnt = db.session.query(cls).filter_by(bug_id=bug_id,
                                              status_when=status_when).count()
        return cnt > 0

    @classmethod
    def get_waiting_patches(cls, bug):
        return [Patch.fromdict(p) for p in bug.get('attachments')
                if p['status'] == 'waiting']

    @classmethod
    def get_waiting_patch_ids(cls, bug):
        return [int(p["id"]) for p in bug.get('attachments')
                if p['status'] == 'waiting']

    def get_patches(self):
        return [int(p) for p in self.patches.split(",")]

    @classmethod
    def verify(cls, bug):
        bug_id = bug['bug_id']
        res = namedtuple("Result", ["result", "msg"])
        bug_branches = Branch.parse_branches(bug.get('branches'))
        supported_branches = Branch.active_branches()

        if not bug_branches:
            msg = "Autoland request for bug %s doesn't specify branches" % \
                  bug_id
            return res(result=False, msg=msg)

        if not set(bug_branches).issubset(set(supported_branches)):
            msg = "Bug %s branches (%s) not supported by Autoland. " \
                  "Supported branches: %s" % (bug_id, ", ".join(bug_branches),
                                              ", ".join(supported_branches))
            return res(result=False, msg=msg)

        req_patches = cls.get_waiting_patches(bug)
        patches = bz.get_patches(bug_id, [x.patch_id for x in req_patches])
        if not patches:
            msg = "No valid patches attached"
            return res(result=False, msg=msg)

        # verify patches against all bug branches
        if not all(Branch.get_by_name(b).patches_applicable(bz, req_patches)
                   for b in bug_branches):
            msg = "Some of the patches do no thave reviews/approvals"
            return res(result=False, msg=msg)

        return res(result=True, msg=None)

    def update_status(self, status):
        self.status = status
        return self


def create_us():
    Base.metadata.create_all(bind=db.engine)
