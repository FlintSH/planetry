"""Authorization follows the configured immutable GitHub user IDs."""

import unittest

from planetry.access import allowed_user, require_policy


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.policies = [
            {'id': 87043024, 'login': 'FlintSH'},
            {'id': 89701169, 'login': 'thebeeee'},
        ]
        self.config = {'allowed_github_users': [dict(user) for user in self.policies]}

    def assert_invalid_policy(self, config):
        with self.assertRaises(ValueError):
            require_policy(config)
        for policy in self.policies:
            self.assertFalse(allowed_user(config, dict(policy, type='User')))

    def test_policy_is_required(self):
        for config in (None, [], 'thebeeee', {}, {'unrelated': True}):
            with self.subTest(config=config):
                self.assert_invalid_policy(config)

    def test_plural_policy_requires_a_nonempty_list(self):
        for users in (None, [], {}, self.policies[0], 'FlintSH', tuple(self.policies)):
            with self.subTest(users=users):
                self.assert_invalid_policy({'allowed_github_users': users})

    def test_plural_policy_authorizes_both_configured_accounts(self):
        self.assertEqual(self.policies, require_policy(self.config))
        for policy in self.policies:
            with self.subTest(policy=policy):
                self.assertTrue(allowed_user(self.config, dict(policy, type='User')))

    def test_legacy_single_policy_is_normalized_and_authorizes_its_account(self):
        for policy in self.policies + [{'id': 901, 'login': 'person-901'}]:
            config = {'allowed_github_user': dict(policy)}
            with self.subTest(policy=policy):
                self.assertEqual([policy], require_policy(config))
                self.assertTrue(allowed_user(config, dict(policy, type='User')))
                self.assertFalse(allowed_user(config, {
                    'id': 12345, 'login': policy['login'], 'type': 'User'}))

    def test_legacy_policy_still_requires_one_valid_object(self):
        for policy in (None, [], self.policies, 'thebeeee', {},
                       {'id': '89701169', 'login': 'thebeeee'},
                       {'id': True, 'login': 'thebeeee'},
                       {'id': 89701169, 'login': 'invalid--login'},
                       dict(self.policies[0], type='User')):
            with self.subTest(policy=policy):
                self.assert_invalid_policy({'allowed_github_user': policy})

    def test_singular_and_plural_keys_are_always_ambiguous(self):
        for users in (self.policies, [], None):
            for user in (self.policies[0], None):
                config = {'allowed_github_users': users, 'allowed_github_user': user}
                with self.subTest(config=config):
                    self.assert_invalid_policy(config)

    def test_policy_requires_exact_positive_integer_ids(self):
        for identity in (None, True, False, '89701169', 89701169.0, 0, -1, [], {}):
            users = [self.policies[0], dict(self.policies[1], id=identity)]
            with self.subTest(identity=identity):
                self.assert_invalid_policy({'allowed_github_users': users})

    def test_malformed_entry_invalidates_the_entire_policy(self):
        for entry in (None, [], 'thebeeee', {}, {'login': 'thebeeee'},
                      {'id': 89701169}, dict(self.policies[1], type='User')):
            for users in ([entry] + self.policies, self.policies + [entry]):
                with self.subTest(users=users):
                    self.assert_invalid_policy({'allowed_github_users': users})

    def test_policy_requires_valid_github_login_metadata(self):
        for login in (None, 123, '', '-user', 'user-', 'user--name', 'user_name',
                      'user.name', 'user/name', 'user name', ' user', 'user ',
                      'a\n', 'é', 'a' * 40):
            users = [self.policies[0], dict(self.policies[1], login=login)]
            with self.subTest(login=login):
                self.assert_invalid_policy({'allowed_github_users': users})

    def test_valid_github_login_boundaries(self):
        for login in ('a', '0', 'FlintSH', 'person-1', 'a-b-c', 'a' * 39):
            policy = {'id': 1, 'login': login}
            with self.subTest(login=login):
                self.assertEqual([policy], require_policy({'allowed_github_users': [policy]}))

    def test_duplicate_ids_are_rejected_even_with_different_logins(self):
        for duplicate in (dict(self.policies[0]),
                          dict(self.policies[0], login='renamed-account')):
            with self.subTest(duplicate=duplicate):
                self.assert_invalid_policy({'allowed_github_users': self.policies + [duplicate]})

    def test_duplicate_logins_are_rejected_case_insensitively(self):
        for login in ('FlintSH', 'flintsh', 'FLINTSH'):
            users = self.policies + [{'id': 12345, 'login': login}]
            with self.subTest(login=login):
                self.assert_invalid_policy({'allowed_github_users': users})

    def test_outsiders_cannot_gain_access_by_copying_a_login_or_repository_role(self):
        for login in ('FlintSH', 'thebeeee', 'outsider'):
            with self.subTest(login=login):
                self.assertFalse(allowed_user(self.config, {
                    'id': 12345, 'login': login, 'type': 'User',
                    'site_admin': True, 'role': 'owner'}))

    def test_renamed_accounts_remain_authorized_by_numeric_id(self):
        for policy in self.policies:
            with self.subTest(policy=policy):
                self.assertTrue(allowed_user(self.config, {
                    'id': policy['id'], 'login': 'renamed-account', 'type': 'User'}))
                self.assertTrue(allowed_user(self.config, {
                    'id': policy['id'], 'type': 'User'}))

    def test_missing_malformed_and_nonhuman_identities_fail_closed(self):
        users = [None, [], 'thebeeee', {},
                 {'login': 'thebeeee', 'type': 'User'},
                 {'id': '89701169', 'login': 'thebeeee', 'type': 'User'},
                 {'id': 89701169.0, 'login': 'thebeeee', 'type': 'User'}]
        for policy in self.policies:
            for user_type in ('Bot', 'Organization', 'user', 'USER', None, True, 1):
                users.append(dict(policy, type=user_type))
            users.append(dict(policy))
        for user in users:
            with self.subTest(user=user):
                self.assertFalse(allowed_user(self.config, user))

    def test_boolean_user_id_cannot_match_numeric_id_one(self):
        for config in ({'allowed_github_users': [{'id': 1, 'login': 'person-1'}]},
                       {'allowed_github_user': {'id': 1, 'login': 'person-1'}}):
            for identity in (True, False):
                with self.subTest(config=config, identity=identity):
                    self.assertFalse(allowed_user(config, {
                        'id': identity, 'login': 'person-1', 'type': 'User'}))


if __name__ == '__main__':
    unittest.main()
