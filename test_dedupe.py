#!/usr/bin/env python3
import unittest

from dedupe_vault import (
    BwError,
    already_moved_rows,
    group_duplicates,
    load_report,
    normalize_host,
    pick_keeper,
    plan_moves,
    public_item_view,
    report_ids,
    require_dedupe_report,
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
        planned = plan_moves([[a, b]], keep="richest", move_groups=False, skip_org=False)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["keeper"]["id"], "a")
        self.assertEqual([m["id"] for m in planned[0]["move"]], ["b"])

    def test_still_plans_when_extras_already_in_review_folder(self):
        keeper = login("k", "Site", "me", ["https://example.com"], totp="x")
        extra = login("e", "Site", "me", ["https://example.com"], folder_id="dup")
        planned = plan_moves(
            [[keeper, extra]],
            keep="richest",
            move_groups=False,
            skip_org=False,
        )
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["keeper"]["id"], "k")
        self.assertEqual([m["id"] for m in planned[0]["move"]], ["e"])

    def test_plans_group_when_all_copies_already_in_review_folder(self):
        a = login("a", "Site", "me", ["https://example.com"], totp="x", folder_id="dup")
        b = login("b", "Site", "me", ["https://example.com"], folder_id="dup")
        planned = plan_moves(
            [[a, b]], keep="richest", move_groups=False, skip_org=False
        )
        self.assertEqual(planned[0]["keeper"]["id"], "a")
        self.assertEqual([m["id"] for m in planned[0]["move"]], ["b"])

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

    def test_missing_report_is_bw_error(self):
        with self.assertRaises(BwError) as ctx:
            load_report("/tmp/definitely-missing-bw-dupes.json")
        msg = str(ctx.exception)
        self.assertIn("not found", msg.lower())
        self.assertIn("does not create", msg.lower())

    def test_already_moved_rows_from_review_folder(self):
        keeper = login("k", "Site", "me", ["https://example.com"], totp="x")
        extra = login("e", "Site", "me", ["https://example.com"], folder_id="dup")
        pending = login("p", "Site", "me", ["https://example.com"])
        planned = plan_moves(
            [[keeper, extra, pending]],
            keep="richest",
            move_groups=False,
            skip_org=False,
        )
        rows = already_moved_rows(planned, "dup")
        self.assertEqual({r["id"] for r in rows}, {"e"})
        self.assertEqual(rows[0]["keeperId"], "k")

    def test_regenerated_report_deletes_filed_extras(self):
        keeper = login("k", "Site", "me", ["https://example.com"], totp="x")
        extra = login("e", "Site", "me", ["https://example.com"], folder_id="dup")
        planned = plan_moves(
            [[keeper, extra]],
            keep="richest",
            move_groups=False,
            skip_org=False,
        )
        report = {
            "strategy": "host-user",
            "folderName": "Duplicates - Review",
            "groups": planned,
            "moved": already_moved_rows(planned, "dup"),
        }
        to_delete, skipped = select_delete_candidates(
            [keeper, extra],
            folder_id="dup",
            report=report,
            skip_org=False,
            include_keepers=False,
        )
        self.assertEqual([i["id"] for i in to_delete], ["e"])
        self.assertEqual(skipped, [])

    def test_empty_report_with_dedupe_shape_explains_regeneration(self):
        with self.assertRaises(BwError) as ctx:
            require_dedupe_report(
                {
                    "strategy": "host-user",
                    "folderName": "Duplicates - Review",
                    "groups": [],
                    "moved": [],
                },
                "/tmp/bw-dupes.json",
            )
        msg = str(ctx.exception)
        self.assertIn("no groups or moved items", msg.lower())
        self.assertIn("omit --report", msg.lower())

    def test_unrelated_json_is_not_a_dedupe_report(self):
        with self.assertRaises(BwError) as ctx:
            require_dedupe_report({"foo": 1}, "/tmp/other.json")
        self.assertIn("does not look like a dedupe report", str(ctx.exception))

    def test_report_with_groups_is_accepted(self):
        require_dedupe_report(
            {"groups": [{"keeper": {"id": "k"}}], "moved": []},
            "/tmp/bw-dupes.json",
        )


if __name__ == "__main__":
    unittest.main()
