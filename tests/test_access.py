"""Authorization follows the configured immutable GitHub user ID."""

import unittest

from planetry.access import allowed_user, require_policy


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.policy = {'id': 89701169, 'login': 'thebeeee'}
        self.config = {'allowed_github_user': dict(self.policy)}

    def test_policy_is_required(self):
        for config in ({}, {'allowed_github_user': None},
                       {'allowed_github_user': []}, {'allowed_github_user': 'thebeeee'}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                require_policy(config)

    def test_policy_requires_exact_positive_integer_id(self):
        for identity in (None, True, False, '89701169', 89701169.0, 0, -1, [], {}):
            config = {'allowed_github_user': dict(self.policy, id=identity)}
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                require_policy(config)
        with self.assertRaises(ValueError):
            require_policy({'allowed_github_user': {'login': 'thebeeee'}})

    def test_policy_requires_login_metadata(self):
        for policy in ({'id': 89701169}, {'id': 89701169, 'login': None},
                       {'id': 89701169, 'login': ''}, {'id': 89701169, 'login': 123}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                require_policy({'allowed_github_user': policy})

    def test_valid_policy_and_matching_human_are_allowed(self):
        self.assertEqual(self.policy, require_policy(self.config))
        self.assertTrue(allowed_user(self.config, {
            'id': 89701169, 'login': 'thebeeee', 'type': 'User'}))

    def test_renamed_account_is_allowed_but_copied_login_is_denied(self):
        self.assertTrue(allowed_user(self.config, {
            'id': 89701169, 'login': 'renamed-account', 'type': 'User'}))
        self.assertFalse(allowed_user(self.config, {
            'id': 12345, 'login': 'thebeeee', 'type': 'User'}))
        self.assertFalse(allowed_user(self.config, {
            'id': 12345, 'login': 'FlintSH', 'type': 'User'}))

    def test_missing_malformed_and_nonhuman_identities_fail_closed(self):
        users = [None, [], 'thebeeee', {},
                 {'login': 'thebeeee', 'type': 'User'},
                 {'id': '89701169', 'login': 'thebeeee', 'type': 'User'},
                 {'id': 89701169.0, 'login': 'thebeeee', 'type': 'User'},
                 {'id': 89701169, 'login': 'thebeeee', 'type': 'Bot'},
                 {'id': 89701169, 'login': 'thebeeee', 'type': 'Organization'},
                 {'id': 89701169, 'login': 'thebeeee'},
                 {'id': 89701169, 'login': 'thebeeee', 'type': None}]
        for user in users:
            with self.subTest(user=user):
                self.assertFalse(allowed_user(self.config, user))

    def test_boolean_user_id_cannot_match_numeric_id_one(self):
        config = {'allowed_github_user': {'id': 1, 'login': 'person-1'}}
        for identity in (True, False):
            with self.subTest(identity=identity):
                self.assertFalse(allowed_user(config, {
                    'id': identity, 'login': 'person-1', 'type': 'User'}))

    def test_invalid_policy_never_authorizes_a_user(self):
        user = {'id': 89701169, 'login': 'thebeeee', 'type': 'User'}
        for config in ({}, {'allowed_github_user': {'id': '89701169', 'login': 'thebeeee'}}):
            with self.subTest(config=config):
                self.assertFalse(allowed_user(config, user))


if __name__ == '__main__':
    unittest.main()
