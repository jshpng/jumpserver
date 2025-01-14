# ~*~ coding: utf-8 ~*~

path_perms_map = {
    'xpack': '*',
    'replay': 'default',
    'applets': 'terminal.view_applet',
    'playbooks': 'ops.view_playbook'
}


def allow_access(private_file):
    request = private_file.request
    request_path = private_file.request.path
    path_list = str(request_path)[1:].split('/')
    path_base = path_list[1] if len(path_list) > 1 else None
    path_perm = path_perms_map.get(path_base, None)

    if not path_perm:
        return False
    if path_perm == '*' or request.user.has_perms([path_perm]):
        return True
    if path_perm == 'default':
        return request.user.is_authenticated and request.user.is_staff
    return False
