"""Operator-owned GitHub identity policy; repository roles never grant access."""

import re


def require_policy(config):
    user = config.get('allowed_github_user') if isinstance(config, dict) else None
    if (not isinstance(user, dict) or set(user) != {'id', 'login'}
            or type(user.get('id')) is not int or user['id'] <= 0
            or not isinstance(user.get('login'), str)
            or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', user['login'])):
        raise ValueError('allowed_github_user requires one positive numeric id and GitHub login')
    return user


def allowed_user(config, user):
    try:
        policy = require_policy(config)
    except ValueError:
        return False
    return (isinstance(user, dict) and user.get('type') == 'User'
            and type(user.get('id')) is int and user['id'] == policy['id'])
