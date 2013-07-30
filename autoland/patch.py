from __future__ import absolute_import
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from autoland.db import Base, db, str_to_datetime
from .bugzilla import bz
from .branch import Branch


class Patch(object):

    def __init__(self, patch_id, reviews=None, approvals=None):
        self.patch_id = patch_id
        self.reviews = reviews
        self.approvals = approvals

    @classmethod
    def fromdict(cls, patch):
        reviews = []
        for flag in patch.get('flags', []):
            if flag.get('name') in ('review', 'superreview', 'ui-review'):
                review = {
                    'type': flag['name'],
                    'reviewer': bz.get_user_info(flag['setter']['name']),
                    'result': flag['status']
                }
                reviews.append(review)
        approvals = []
        for flag in patch.get('flags', []):
            a_type = flag.get('name')
            if not a_type:
                continue
            if a_type.startswith("approval-"):
                approval = {
                    'type': a_type.replace("approval-", ""),
                    'approver': bz.get_user_info(flag['setter']['name']),
                    'result': flag['status']
                }
                approvals.append(approval)
        return cls(patch_id=patch["id"], reviews=reviews, approvals=approvals)


class PatchSet(Base):
    __tablename__ = "patchset"
    id = Column(Integer, primary_key=True)
    bug_id = Column(Integer)
    branch_id = Column(Integer, ForeignKey('branch.id'))
    try_syntax = Column(String)
    status_when = Column(DateTime)
    status = Column(String)
    _patches = Column("patches", String)
    queued = Column(Boolean, default=False)
    applied = Column(Boolean, default=False)
    failed = Column(Boolean, default=False)
    push_time = Column(DateTime)
    changeset = Column(String)

    branch = relationship("Branch")

    def __init__(self, bug_id, patches, branch, status_when, try_syntax=None):
        self.bug_id = bug_id
        assert all(isinstance(i, int) for i in patches), "Only int accepted"
        self._patches = ",".join(str(p) for p in patches)

        if isinstance(branch, int):
            self.branch = Branch.get_by_id(branch)
        elif isinstance(branch, basestring):
            self.branch = Branch.get_by_name(branch)
        else:
            self.branch = branch
        self.try_syntax = try_syntax
        self.status_when = str_to_datetime(status_when)

    @classmethod
    def fromdict(cls, ps):
        return cls(bug_id=ps["bug_id"], patches=ps["patches"],
                   branch=ps["branch"], status_when=ps["status_when"],
                   try_syntax=ps["try_syntax"])

    @property
    def patches(self):
        return [int(p) for p in self._patches.split(",")]

    @patches.setter
    def patches(self, patches):
        assert all(isinstance(i, int) for i in patches), "Only int accepted"
        self._patches = ",".join(str(p) for p in patches)

    def __iter__(self):
        return iter(self.patches)

    def save(self):
        if self not in db.session:
            db.session.add(self)
        db.session.commit()

    def update_status(self, status):
        self.status = status
        return self

    @classmethod
    def processed(cls, bug_id, branch, status_when):
        status_when = str_to_datetime(status_when)
        cnt = db.session.query(cls).filter(
            cls.bug_id == bug_id, cls.status_when == status_when,
            cls.status != None
        ).filter(Branch.name == branch).count()
        return cnt > 0


def create_us():
    Base.metadata.create_all(bind=db.engine)
