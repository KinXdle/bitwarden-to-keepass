import json
import logging
import os
import re
import subprocess
import datetime
from argparse import ArgumentParser
from typing import Dict, List, Optional

from pykeepass import PyKeePass, create_database
from pykeepass.exceptions import CredentialsError
from pykeepass.group import Group as KPGroup
from dateutil import parser
import folder as FolderType
from item import Item, Types as ItemTypes

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s :: %(levelname)s :: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

kp: Optional[PyKeePass] = None
CARD_FOLDER_ID = 'F6C4255D-9260-4407-A4F6-AF773D179F67'


def bitwarden_to_keepass(args):
    global kp
    try:
        kp = PyKeePass(args.database_path, password=args.database_password, keyfile=args.database_keyfile)
    except FileNotFoundError:
        logging.info('KeePass database does not exist, creating a new one.')
        kp = create_database(args.database_path, password=args.database_password, keyfile=args.database_keyfile)
    except CredentialsError as e:
        logging.error(f'Wrong password for KeePass database: {e}')
        return

    sync = subprocess.check_output([args.bw_path, 'sync', '--session', args.bw_session], encoding='utf8')

    folders = subprocess.check_output([args.bw_path, 'list', 'folders', '--session', args.bw_session], encoding='utf8')
    folders = json.loads(folders)
    groups_by_id = load_folders(folders)
    logging.info(f'Folders done ({len(groups_by_id)}).')

    items = subprocess.check_output([args.bw_path, 'list', 'items', '--session', args.bw_session], encoding='utf8')
    items = json.loads(items)
    logging.info(f'Starting to process {len(items)} items.')
    for item in items:

        bw_item = Item(item)

        is_duplicate_title = False

        create_time = None
        modify_time = None

        while True:
            entry_title = bw_item.get_name() if not is_duplicate_title else '{name} - ({item_id}'.format(
                name=bw_item.get_name(), item_id=bw_item.get_id())
            try:
                if item['type'] in [ItemTypes.CARD, ItemTypes.IDENTITY]:
                    entry = kp.add_entry(
                        destination_group=groups_by_id[CARD_FOLDER_ID],
                        title=entry_title,
                        username=bw_item.item['card']['cardholderName'],
                        password=bw_item.item['card']['number'],
                        notes=bw_item.get_notes()
                    )
                    card = bw_item.get_card()
                    entry.set_custom_property('_BW_CARD_BRAND', card.get_brand(), True)
                    entry.set_custom_property('_BW_CARD_EXP_YEAR', card.get_exp_year(), True)
                    entry.set_custom_property('_BW_CARD_EXP_MONTH', card.get_exp_month(), True)
                    entry.set_custom_property('_BW_CARD_CVV', card.get_code(), True)
                else:
                    entry = kp.add_entry(
                        destination_group=groups_by_id[bw_item.get_folder_id()],
                        title=entry_title,
                        username=bw_item.get_username(),
                        password=bw_item.get_password(),
                        notes=bw_item.get_notes()
                    )
                    # password history
                    if 'passwordHistory' in bw_item.item:
                        history_str = ''
                        for pass_history in bw_item.item['passwordHistory']:
                            _lastUsedDate = pass_history['lastUsedDate']
                            _password_history = pass_history['password']
                            history_str = history_str + "{}\t{}".format(_lastUsedDate, _password_history) + '\n'
                            _lastUsedDate_date = datetime.datetime.strptime(_lastUsedDate, "%Y-%m-%dT%H:%M:%S.%fZ")
                            if create_time is None:
                                create_time = _lastUsedDate_date
                            elif create_time > _lastUsedDate_date:
                                create_time = _lastUsedDate_date

                            if modify_time is None:
                                modify_time = _lastUsedDate_date
                            elif modify_time < _lastUsedDate_date:
                                modify_time = _lastUsedDate_date

                        entry.set_custom_property('_BW_PASSWORD_HISTORY', history_str, True)
                break
            except Exception as e:
                if 'already exists' in str(e):
                    is_duplicate_title = True
                    continue
                raise

        totp_secret, totp_settings = bw_item.get_totp()
        if totp_secret and totp_settings:
            entry.set_custom_property('TOTP Seed', totp_secret, True)
            entry.set_custom_property('TOTP Settings', totp_settings, True)

        for index, uri in enumerate(bw_item.get_uris()):
            if index == 0:
                entry.url = uri['uri']
            else:
                entry.set_custom_property('KP2A_URL_' + str(index), uri['uri'])

        for field in bw_item.get_custom_fields():
            entry.set_custom_property(field['name'], field['value'])

        for attachment in bw_item.get_attachments():
            attachment_raw = subprocess.check_output([
                args.bw_path, 'get', 'attachment', attachment['id'], '--raw', '--itemid', bw_item.get_id(),
                '--session', args.bw_session,
            ])
            attachment_id = kp.add_binary(attachment_raw)
            entry.add_attachment(attachment_id, attachment['fileName'])

        # add bw_id
        entry.set_custom_property('_BW_ID', bw_item.get_id())
        # add bw_folder_id
        if bw_item.get_folder_id():
            entry.set_custom_property('_BW_FOLDER_ID', bw_item.get_folder_id())
        # set date
        bw_modify_date = datetime.datetime.strptime(bw_item.item['revisionDate'], "%Y-%m-%dT%H:%M:%S.%fZ")
        if create_time is None:
            create_time = bw_modify_date
        if modify_time is None:
            modify_time = bw_modify_date
        elif modify_time < bw_modify_date:
            modify_time = bw_modify_date

        entry.ctime = create_time
        entry.mtime = modify_time
    logging.info('Saving changes to KeePass database.')
    kp.save()
    logging.info('Export completed.')


def load_folders(folders) -> Dict[str, KPGroup]:
    # sort folders so that in the case of nested folders, the parents would be guaranteed to show up before the children
    folders.sort(key=lambda x: x['name'])

    # dict to store mapping of Bitwarden folder id to keepass group
    groups_by_id: Dict[str, KPGroup] = {}

    # build up folder tree
    folder_root: FolderType.Folder = FolderType.Folder(None)
    folder_root.keepass_group = kp.root_group
    groups_by_id[None] = kp.root_group

    for folder in folders:
        if folder['id'] is not None:
            new_folder: FolderType.Folder = FolderType.Folder(folder['id'])
            # regex lifted from https://github.com/bitwarden/jslib/blob/ecdd08624f61ccff8128b7cb3241f39e664e1c7f/common/src/services/folder.service.ts#L108
            folder_name_parts: List[str] = re.sub(r'^\/+|\/+$', '', folder['name']).split('/')
            FolderType.nested_traverse_insert(folder_root, folder_name_parts, new_folder, '/')

    # create keepass groups based off folder tree
    def add_keepass_group(folder: FolderType.Folder):
        parent_group: KPGroup = folder.parent.keepass_group
        new_group: KPGroup = kp.add_group(parent_group, folder.name)
        folder.keepass_group = new_group
        groups_by_id[folder.id] = new_group

    # add card folder
    new_folder: FolderType.Folder = FolderType.Folder(CARD_FOLDER_ID)
    FolderType.nested_traverse_insert(folder_root, ['支付卡'], new_folder, '/')

    FolderType.bfs_traverse_execute(folder_root, add_keepass_group)

    return groups_by_id


def check_args(args):
    if args.database_keyfile:
        if not os.path.isfile(args.database_keyfile) or not os.access(args.database_keyfile, os.R_OK):
            logging.error('Key File for KeePass database is not readable.')
            return False

    if not os.path.isfile(args.bw_path) or not os.access(args.bw_path, os.X_OK):
        logging.error('bitwarden-cli was not found or not executable. Did you set correct \'--bw-path\'?')
        return False

    return True


def environ_or_required(key):
    return (
        {'default': os.environ.get(key)} if os.environ.get(key)
        else {'required': True}
    )


parser = ArgumentParser()
parser.add_argument(
    '--bw-session',
    help='Session generated from bitwarden-cli (bw login)',
    **environ_or_required('BW_SESSION'),
)
parser.add_argument(
    '--database-path',
    help='Path to KeePass database. If database does not exists it will be created.',
    **environ_or_required('DATABASE_PATH'),
)
parser.add_argument(
    '--database-password',
    help='Password for KeePass database',
    **environ_or_required('DATABASE_PASSWORD'),
)
parser.add_argument(
    '--database-keyfile',
    help='Path to Key File for KeePass database',
    default=os.environ.get('DATABASE_KEYFILE', None),
)
parser.add_argument(
    '--bw-path',
    help='Path for bw binary',
    default=os.environ.get('BW_PATH', 'bw'),
)
args = parser.parse_args()

check_args(args) and bitwarden_to_keepass(args)
