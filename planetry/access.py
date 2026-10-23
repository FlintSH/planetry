"""Operator-owned GitHub identity policy; repository roles never grant access."""

import re


def require_policy(config):
    if not isinstance(config, dict):
        raise ValueError('allowed_github_users is required')
    if 'allowed_github_users' in config and 'allowed_github_user' in config:
        raise ValueError('allowed_github_users and allowed_github_user cannot be configured together')
    if 'allowed_github_users' in config:
        users = config['allowed_github_users']
    elif 'allowed_github_user' in config:
        users = [config['allowed_github_user']]
    else:
        raise ValueError('allowed_github_users is required')
    if not isinstance(users, list) or not users:
        raise ValueError('allowed_github_users requires a nonempty list')

    ids = set()
    logins = set()
    policy = []
    for user in users:
        if (not isinstance(user, dict) or set(user) != {'id', 'login'}
                or type(user.get('id')) is not int or user['id'] <= 0
                or not isinstance(user.get('login'), str)
                or not re.fullmatch(
                    r'(?!.*--)[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?',
                    user['login'])):
            raise ValueError('GitHub users require exactly one positive integer id and valid login')
        login = user['login'].lower()
        if user['id'] in ids or login in logins:
            raise ValueError('GitHub user ids and logins must be unique')
        ids.add(user['id'])
        logins.add(login)
        policy.append({'id': user['id'], 'login': user['login']})
    return policy


def allowed_user(config, user):
    try:
        policy = require_policy(config)
    except ValueError:
        return False
    return (isinstance(user, dict) and user.get('type') == 'User'
            and type(user.get('id')) is int
            and any(user['id'] == identity['id'] for identity in policy))
