from autoland.db import Base, db
from autoland.patch import PatchSet
from autoland.branch import Branch
from autoland.request import AutolandRequest

Base.metadata.create_all(bind=db.engine)

# sample data
if False:
    b = Branch("tools")
    b.pull_repo = "https://hg.mozilla.org/users/raliiev_mozilla.com/tools-for-autoland"
    b.push_repo = "ssh://hg.mozilla.org/users/raliiev_mozilla.com/tools-for-autoland"
    #b.tbpl_name = "Try"
    b.enabled = True
    b.approval_required = False
    b.review_required = False
    b.add_try_commit = False
    b.use_tree_status = False
    db.session.add(b)
    db.session.commit()
