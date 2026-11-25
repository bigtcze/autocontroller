"""NoCloud seed rendering. User-data is #cloud-config expressed as JSON (valid YAML)
so no hand-written YAML escaping is needed. No passwords exist anywhere in it.
"""
from __future__ import annotations

import json

CONTROLLER_USER = 'tester'
ADMIN_USER = 'vmadmin'
# Installed by the harness only in the disposable guest. Never a production host.
NOPASSWD_SUDO = 'ALL=(ALL) NOPASSWD:ALL'
# git is preinstalled on Ubuntu cloud images; the scenario must start without it.
PURGE_PACKAGES = ('git',)


def user_data(*, client_pubkey: str, host_private: str, host_public: str) -> str:
    for label, value in (('client_pubkey', client_pubkey), ('host_public', host_public)):
        if not value.startswith('ssh-'):
            raise ValueError(f'{label} must be an OpenSSH public key line')
    if 'PRIVATE KEY' not in host_private:
        raise ValueError('host_private must be a PEM/OpenSSH private key')
    users = [
        {'name': CONTROLLER_USER, 'shell': '/bin/bash', 'create_groups': True,
         'sudo': NOPASSWD_SUDO, 'lock_passwd': True, 'ssh_authorized_keys': [client_pubkey]},
        {'name': ADMIN_USER, 'shell': '/bin/bash', 'create_groups': True,
         'sudo': NOPASSWD_SUDO, 'lock_passwd': True, 'ssh_authorized_keys': [client_pubkey]},
    ]
    purge = ' '.join(PURGE_PACKAGES)
    doc = {
        'hostname': 'autocontroller-vmtest',
        'manage_etc_hosts': True,
        'package_update': False,
        'package_upgrade': False,
        'ssh_pwauth': False,
        'disable_root': True,
        'users': users,
        'ssh_keys': {'ed25519_private': host_private, 'ed25519_public': host_public},
        'runcmd': [
            ['sh', '-c', f'if dpkg -s {purge} >/dev/null 2>&1; then DEBIAN_FRONTEND=noninteractive apt-get purge -y {purge}; fi'],
            ['sh', '-c', 'touch /var/lib/vmtest-cloud-init-done'],
        ],
    }
    return '#cloud-config\n' + json.dumps(doc, indent=1, sort_keys=True) + '\n'


def meta_data(instance_id: str) -> str:
    if not instance_id.replace('-', '').isalnum():
        raise ValueError('instance id must be alphanumeric')
    return json.dumps({'instance-id': instance_id, 'local-hostname': 'autocontroller-vmtest'}, sort_keys=True) + '\n'


def smbios_argument(port: int) -> str:
    """NoCloud-net seed location. 10.0.2.2 is the QEMU slirp alias for the host loopback."""
    if not 1024 <= port <= 65535:
        raise ValueError('seed port out of range')
    return f'type=1,serial=ds=nocloud;s=http://10.0.2.2:{port}/'
