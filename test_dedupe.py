#!/usr/bin/env python3
import unittest

from dedupe_vault import (
    group_duplicates,
    normalize_host,
    pick_keeper,
    plan_moves,
    public_item_view,
    report_ids,
    richness,
    select_delete_candidates,
)


def login(
    item_id: str,
    name: str,
    username: str,
    uris: list[str],
    *,
    password: str = "secret",
    totp: str | None = None,
    notes: str | None = None,
    favorite: bool = False,
    folder_id: str | None = None,
    organization_id: str | None = None,
    revision: str = "2024-01-02T00:00:00.000Z",
    created: str = "2023-01-01T00:00:00.000Z",
) -> dict:
    return {
        "id": item_id,
        "type": 1,
        "name": name,
        "notes": notes,
        "favorite": favorite,
        "folderId": folder_id,
        "organizationId": organization_id,
        "revisionDate": revision,
        "creationDate": created,
        "login": {
            "username": username,
            "password": password,
            "totp": totp,
            "uris": [{"uri": u} for u in uris],
        },
        "fields": [],
        "attachments": [],
    }


class NormalizeHostTests(unittest.TestCase):
    def test_strips_www_and_scheme(self):
        self.assertEqual(normalize_host("https://www.GitHub.com/login"), "github.com")
        self.assertEqual(normalize_host("github.com"), "github.com")
        self.assertIsNone(normalize_host(""))
        self.assertIsNone(normalize_host(None))


class GroupTests(unittest.TestCase):
    def test_host_user_groups_same_site_and_user(self):
        items = [
            login("a", "GitHub", "me", ["https://github.com"]),
            login("b", "GH", "me", ["https://www.github.com/login"]),
            login("c", "GitLab", "me", ["https://gitlab.com"]),
        ]
        groups = group_duplicates(items, "host-user")
        self.assertEqual(len(groups), 1)
        ids = {i["id"] for i in groups[0]}
        self.assertEqual(ids, {"a", "b"})

    def test_overlapping_hosts_merge(self):
        items = [
            login("a", "Google", "me", ["https://google.com"]),
            login("b", "Google", "me", ["https://google.com", "https://youtube.com"]),
            login("c", "YouTube", "me", ["https://youtube.com"]),
        ]
        groups = group_duplicates(items, "host-user")
        self.assertEqual(len(groups), 1)
        self.assertEqual({i["id"] for i in groups[0]}, {"a", "b", "c"})

    def test_skips_empty_username_by_default(self):
        items = [
            login("a", "Site", "", ["https://example.com"]),
            login("b", "Site", "", ["https://example.com"]),
        ]
        self.assertEqual(group_duplicates(items, "host-user"), [])
        groups = group_duplicates(items, "host-user", include_empty_usernames=True)
        self.assertEqual(len(groups), 1)

    def test_name_user_strategy(self):
        items = [
            login("a", "Bank", "me", ["https://a.example"]),
            login("b", "Bank", "me", ["https://b.example"]),
            login("c", "Bank", "other", ["https://a.example"]),
        ]
        groups = group_duplicates(items, "name-user")
        self.assertEqual(len(groups), 1)
        self.assertEqual({i["id"] for i in groups[0]}, {"a", "b"})

    def test_exact_requires_same_password(self):
        items = [
            login("a", "Site", "me", ["https://example.com"], password="one"),
            login("b", "Site", "me", ["https://example.com"], password="two"),
            login("c", "Site", "me", ["https://example.com"], password="one"),
        ]
        groups = group_duplicates(items, "exact")
        self.assertEqual(len(groups), 1)
        self.assertEqual({i["id"] for i in groups[0]}, {"a", "c"})

    def test_ignores_non_logins(self):
        note = {"id": "n", "type": 2, "name": "note"}
        items = [login("a", "Site", "me", ["https://example.com"]), note]
        self.assertEqual(group_duplicates(items, "host-user"), [])


class KeeperTests(unittest.TestCase):
    def test_richest_prefers_totp(self):
        plain = login("a", "Site", "me", ["https://example.com"])
        totp = login("b", "Site", "me", ["https://example.com"], totp="otpauth://")
        self.assertEqual(pick_keeper([plain, totp], "richest")["id"], "b")
        self.assertGreater(richness(totp), richness(plain))

    def test_newest_uses_revision(self):
        old = login(
            "a",
            "Site",
            "me",
            ["https://example.com"],
            revision="2020-01-01T00:00:00Z",
            created="2019-01-01T00:00:00Z",
        )
        new = login(
            "b",
            "Site",
            "me",
            ["https://example.com"],
            revision="2024-01-01T00:00:00Z",
            created="2023-01-01T00:00:00Z",
        )
        self.assertEqual(pick_keeper([old, new], "newest")["id"], "b")
        self.assertEqual(pick_keeper([old, new], "oldest")["id"], "a")


class PlanTests(unittest.TestCase):
    def test_moves_extras_keeps_one(self):
        a = login("a", "Site", "me", ["https://example.com"], totp="x")
        b = login("b", "Site", "me", ["https://example.com"])
        planned = plan_moves([[a, b]], keep="richest", move_groups=False, skip_folder_id=None, skip_org=False)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["keeper"]["id"], "a")
        self.assertEqual([m["id"] for m in planned[0]["move"]], ["b"])

    def test_skips_items_already_in_review_folder(self):
        a = login("a", "Site", "me", ["https://example.com"], folder_id="dup")
        b = login("b", "Site", "me", ["https://example.com"], folder_id="dup")
        planned = plan_moves([[a, b]], keep="richest", move_groups=False, skip_folder_id="dup", skip_org=False)
        self.assertEqual(planned, [])

    def test_public_view_strips_secrets(self):
        item = login("a", "Site", "me", ["https://example.com"], totp="otpauth://secret")
        view = public_item_view(item)
        dumped = str(view)
        self.assertNotIn("secret", dumped)
        self.assertNotIn("otpauth", dumped)
        self.assertTrue(view["hasTotp"])
        self.assertTrue(view["hasPassword"])


class DeleteCandidateTests(unittest.TestCase):
    def test_without_report_deletes_everything_in_folder(self):
        a = login("a", "Site", "me", ["https://example.com"], folder_id="dup")
        b = login("b", "Other", "me", ["https://other.com"], folder_id="keep")
        to_delete, skipped = select_delete_candidates(
            [a, b], folder_id="dup", report=None, skip_org=False, include_keepers=False
        )
        self.assertEqual([i["id"] for i in to_delete], ["a"])
        self.assertEqual(skipped, [])

    def test_report_skips_keepers_and_unlisted_items(self):
        keeper = login("k", "Site", "me", ["https://example.com"], folder_id="dup")
        extra = login("e", "Site", "me", ["https://example.com"], folder_id="dup")
        other = login("o", "Unrelated", "x", ["https://x.com"], folder_id="dup")
        restored = login("r", "Site", "me", ["https://example.com"], folder_id="inbox")
        report = {
            "groups": [{"keeper": {"id": "k"}}],
            "moved": [
                {"id": "e", "keeperId": "k"},
                {"id": "r", "keeperId": "k"},
            ],
        }
        to_delete, skipped = select_delete_candidates(
            [keeper, extra, other, restored],
            folder_id="dup",
            report=report,
            skip_org=False,
            include_keepers=False,
        )
        self.assertEqual([i["id"] for i in to_delete], ["e"])
        self.assertEqual([i["id"] for i in skipped], ["k"])

    def test_include_keepers(self):
        keeper = login("k", "Site", "me", ["https://example.com"], folder_id="dup")
        extra = login("e", "Site", "me", ["https://example.com"], folder_id="dup")
        report = {
            "groups": [{"keeper": {"id": "k"}}],
            "moved": [{"id": "e", "keeperId": "k"}, {"id": "k", "keeperId": "k"}],
        }
        to_delete, skipped = select_delete_candidates(
            [keeper, extra],
            folder_id="dup",
            report=report,
            skip_org=False,
            include_keepers=True,
        )
        self.assertEqual({i["id"] for i in to_delete}, {"k", "e"})
        self.assertEqual(skipped, [])

    def test_report_ids(self):
        moved, keepers = report_ids(
            {
                "groups": [{"keeper": {"id": "k"}}],
                "moved": [{"id": "e", "keeperId": "k"}],
            }
        )
        self.assertEqual(moved, {"e"})
        self.assertEqual(keepers, {"k"})


if __name__ == "__main__":
    unittest.main()
