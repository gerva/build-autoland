import unittest
from autoland.mercurial import strip_reviews, add_reviews, add_approvals, \
    generate_commit_message, generate_default_commit_message

reviews = [
    {
        "type": "review",
        "reviewer": {"email": "user1@example"}
    },
    {
        "type": "superreview",
        "reviewer": {"email": "user2@example"}
    },
    {
        "type": "ui-review",
        "reviewer": {"email": "user3@example"}
    },
    {
        "type": "review",
        "reviewer": {"email": "user4@example"}
    },
]
approvals = [
    {
        "type": "branch1",
        "approver": {"email": "user1@example"},
        "result": "+"
    },
    {
        "type": "branch2",
        "approver": {"email": "user2@example"},
        "result": "+"
    },
    {
        "type": "branch3",
        "approver": {"email": "user3@example"},
        "result": "+"
    },
    {
        "type": "branch1",
        "approver": {"email": "user4@example"},
        "result": "+"
    },
]


class TestAutolandMercurial(unittest.TestCase):

    def test_strip_reviews1(self):
        self.assertEqual(strip_reviews("Message with r=me"), "Message with")

    def test_strip_reviews2(self):
        self.assertEqual(strip_reviews("Message with r=me,not.me,another@one"),
                         "Message with")

    def test_strip_reviews3(self):
        self.assertEqual(strip_reviews("Message with r=me a=not.me"),
                         "Message with")

    def test_strip_reviews4(self):
        self.assertEqual(strip_reviews("Message with r=me a=not.me DONTBUILD"),
                         "Message with DONTBUILD")

    def test_add_reviews(self):
        self.assertEqual(
            add_reviews("Stripped messsage", reviews),
            "Stripped messsage r=user1@example sr=user2@example "
            "ui-r=user3@example r=user4@example")

    def test_add_approvals(self):
        self.assertEqual(
            add_approvals("Stripped messsage", "branch1", approvals),
            "Stripped messsage a=user1@example,user4@example")

    def test_generate_commit_message(self):
        msg = """Commit message r=aaaa a=bbb
        another line"""
        patch = {"reviews": reviews, "approvals": approvals}
        branch = "branch1"
        self.assertEqual(
            generate_commit_message(msg, patch, branch),
            "Commit message r=user1@example sr=user2@example "
            "ui-r=user3@example r=user4@example a=user1@example,user4@example")

    def test_generate_default_commit_message(self):
        bug = {
            "bug_id": 1234, "summary": "Bug title"
        }
        self.assertEqual(generate_default_commit_message(bug),
                         "Bug 1234 - Bug title")
