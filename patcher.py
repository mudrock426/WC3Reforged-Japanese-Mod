"""
Warcraft III: Reforged Japanese Localization Patcher (End-User Installer/Uninstaller)
Reads the user's local CASC game data, applies Japanese translations & custom fonts
to _retail_\\ (Allow Local Files = 1), and records installed files for clean uninstall.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import uuid
import winreg

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import wc3_casc_lib as casc_lib
import wc3_parsers as parsers

WC3_REG_PATH = r'Software\Blizzard Entertainment\Warcraft III'


def find_game_dir() -> str | None:
    """Auto-detects Warcraft III installation directory from Registry or common paths."""
    candidates = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (
            r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Warcraft III',
            r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Warcraft III',
            WC3_REG_PATH,
        ):
            try:
                with winreg.OpenKey(root, sub) as k:
                    for val_name in ('InstallLocation', 'InstallPath', 'GamePath'):
                        try:
                            val, _ = winreg.QueryValueEx(k, val_name)
                            if val and isinstance(val, str):
                                candidates.append(val.strip('"'))
                        except OSError:
                            pass
            except OSError:
                pass
    candidates.extend([
        r'C:\Program Files (x86)\Warcraft III',
        r'C:\Program Files\Warcraft III',
        r'D:\Games\Warcraft III',
        r'E:\Games\Warcraft III',
        r'J:\Games\Warcraft III',
    ])
    for c in candidates:
        if c and os.path.isdir(os.path.join(c, 'Data')) and os.path.isdir(os.path.join(c, '_retail_')):
            return c
    return None


def get_allow_local_files():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WC3_REG_PATH) as k:
            val, _ = winreg.QueryValueEx(k, 'Allow Local Files')
            return int(val)
    except OSError:
        return None


def set_allow_local_files(value: int):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WC3_REG_PATH) as k:
        winreg.SetValueEx(k, 'Allow Local Files', 0, winreg.REG_DWORD, int(value) & 0xFFFFFFFF)


def restore_allow_local_files(prev_val):
    if prev_val is None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WC3_REG_PATH, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, 'Allow Local Files')
        except OSError:
            pass
    else:
        # Write back the exact previous value (not just on/off).
        set_allow_local_files(int(prev_val))


JOURNAL_NAME = 'wc3_ja_journal.json'
TX_BACKUP_NAME = '.wc3_ja_tx_backup'


def _write_json_atomic(path: str, obj):
    tmp = path + '.ja_tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _copy_atomic(src: str, dst: str):
    """Copy so that dst is either absent/old or complete, never half-written."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + '.ja_tmp'
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _abort_unreadable(path: str, err):
    print(f'ERROR: 記録ファイル {path} を読み込めませんでした ({err})。')
    print('元のファイルのバックアップを守るため、処理を中止しました。')
    print('このファイルを修復するか、作者にご連絡ください。')
    sys.exit(1)


def _is_inside(path: str, root: str) -> bool:
    """True if path resolves to a location strictly inside root (no '..' escapes, same drive)."""
    try:
        p = os.path.normcase(os.path.realpath(path))
        r = os.path.normcase(os.path.realpath(root))
        return p != r and os.path.commonpath([p, r]) == r
    except ValueError:  # e.g. different drives on Windows
        return False


def _require_inside(paths, root: str, record_path: str):
    """Abort if a record lists any path outside the folder this patcher is allowed to touch."""
    bad = [p for p in paths if not _is_inside(p, root)]
    if bad:
        print(f'ERROR: 記録ファイル {record_path} に、ゲームフォルダの外を指すパスが含まれています。')
        for p in bad[:5]:
            print(f'    {p}')
        print('安全のため、ファイルの削除・復元を行わずに処理を中止しました。')
        print('ゲームフォルダを移動した場合は元の場所に戻すか、作者にご連絡ください。')
        sys.exit(1)


def _load_record(path: str, list_keys: tuple[str, ...]) -> dict | None:
    """Loads a manifest/journal. Missing file -> None. Broken or wrong shape -> abort."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError('JSON object expected')
        prev = data.get('prev_allow_local_files')
        if prev is not None and (isinstance(prev, bool) or not isinstance(prev, int)):
            raise ValueError('"prev_allow_local_files" must be an integer or null')
        for k in list_keys:
            v = data.get(k, [])
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(f'"{k}" must be a list of paths')
        return data
    except (OSError, ValueError) as e:  # json.JSONDecodeError is a ValueError
        _abort_unreadable(path, e)


def load_manifest(retail_dir: str) -> dict | None:
    path = os.path.join(retail_dir, 'wc3_ja_manifest.json')
    manifest = _load_record(path, ('installed_files', 'backed_up_files'))
    if manifest is not None:
        listed = manifest.get('installed_files', []) + manifest.get('backed_up_files', [])
        if isinstance(manifest.get('restoring'), str):
            listed.append(manifest['restoring'])
        _require_inside(listed, retail_dir, path)
    return manifest


def recover_interrupted_install(retail_dir: str, manifest: dict | None):
    """
    Finishes or undoes an install that was cut off (power loss, forced close).

    The journal is written before any file is touched and removed only after the new
    manifest is saved. The manifest and the journal share a transaction id:
      - same id    -> the new manifest was committed; only cleanup was left to do.
      - different  -> nothing was committed; undo every change listed in the journal.
    """
    journal_path = os.path.join(retail_dir, JOURNAL_NAME)
    tx_dir = os.path.join(retail_dir, TX_BACKUP_NAME)
    backup_dir = os.path.join(retail_dir, '.wc3_ja_backup')
    journal = _load_record(journal_path, ('created', 'backed_up', 'stale_baks'))
    if journal is not None:
        _require_inside(journal['created'] + journal['backed_up'], retail_dir, journal_path)
        _require_inside(journal['stale_baks'], backup_dir, journal_path)

    if journal is None:
        if os.path.isdir(tx_dir):
            # A transaction folder without a journal was left by an older patcher version,
            # so there is no record of whether it is still needed. Never delete it: move it
            # aside under a unique name and tell the user.
            legacy = f'{tx_dir}_legacy_{time.strftime("%Y%m%d_%H%M%S")}'
            os.replace(tx_dir, legacy)
            print(f'[!] 旧バージョンの一時バックアップが残っていたため、削除せずに次の場所へ移しました: {legacy}')
            print('    MOD が正常に動作していれば、このフォルダは削除しても問題ありません。')
        return

    committed = manifest is not None and manifest.get('txid') == journal.get('txid')
    if committed:
        for bak_p in journal['stale_baks']:
            if os.path.exists(bak_p):
                os.remove(bak_p)
    else:
        print('[!] 前回の処理が途中で中断されていたため、元の状態に戻しています...')
        prev_installed = set(manifest.get('installed_files', [])) if manifest else set()
        # 1. Files of the previous version that were overwritten or removed.
        if os.path.isdir(tx_dir):
            for dirpath, _, filenames in os.walk(tx_dir):
                for fn in filenames:
                    if fn.endswith('.ja_tmp'):
                        continue
                    tx_p = os.path.join(dirpath, fn)
                    dst_p = os.path.join(retail_dir, os.path.relpath(tx_p, tx_dir))
                    os.makedirs(os.path.dirname(dst_p), exist_ok=True)
                    os.replace(tx_p, dst_p)
        # 2. Files this run created where nothing existed before.
        for p in journal['created']:
            if p not in prev_installed and os.path.exists(p):
                os.remove(p)
        # 3. Original files this run backed up before overwriting them.
        for p in journal['backed_up']:
            bak_p = os.path.join(backup_dir, os.path.relpath(p, retail_dir))
            if os.path.exists(bak_p):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                os.replace(bak_p, p)
        if manifest is None:
            restore_allow_local_files(journal.get('prev_allow_local_files'))
    shutil.rmtree(tx_dir, ignore_errors=True)
    os.remove(journal_path)


def install(game_dir: str):
    retail_dir = os.path.join(game_dir, '_retail_')
    backup_dir = os.path.join(retail_dir, '.wc3_ja_backup')
    manifest_path = os.path.join(retail_dir, 'wc3_ja_manifest.json')
    journal_path = os.path.join(retail_dir, JOURNAL_NAME)
    tx_backup_dir = os.path.join(retail_dir, TX_BACKUP_NAME)
    trans_path = os.path.join(BASE_DIR, 'data', 'translations.json')
    with open(trans_path, 'r', encoding='utf-8') as f:
        translations = json.load(f)

    old_manifest = load_manifest(retail_dir)
    recover_interrupted_install(retail_dir, old_manifest)

    prev_installed = set()
    backed_up = set()
    prev_reg = get_allow_local_files()
    if old_manifest is not None:
        prev_installed = set(old_manifest.get('installed_files', []))
        backed_up = set(old_manifest.get('backed_up_files', []))
        if 'prev_allow_local_files' in old_manifest:
            prev_reg = old_manifest['prev_allow_local_files']

    txid = uuid.uuid4().hex
    journal = {'txid': txid, 'prev_allow_local_files': prev_reg,
               'created': [], 'backed_up': [], 'stale_baks': []}
    os.makedirs(retail_dir, exist_ok=True)
    _write_json_atomic(journal_path, journal)

    def journal_add(key: str, value: str):
        journal[key].append(value)
        _write_json_atomic(journal_path, journal)

    installed = set()
    created = set()
    newly_backed_up = set()
    tx_backed_up = set()

    def rollback_on_error():
        for p in installed - prev_installed:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        for p in newly_backed_up:
            rel = os.path.relpath(p, retail_dir)
            bak_p = os.path.join(backup_dir, rel)
            if os.path.exists(bak_p):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                os.replace(bak_p, p)
        for p in tx_backed_up:
            rel = os.path.relpath(p, retail_dir)
            tx_p = os.path.join(tx_backup_dir, rel)
            if os.path.exists(tx_p):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                os.replace(tx_p, p)
        if os.path.isdir(tx_backup_dir):
            shutil.rmtree(tx_backup_dir, ignore_errors=True)
        if not prev_installed:
            restore_allow_local_files(prev_reg)
        try:
            os.remove(journal_path)
        except OSError:
            pass

    def backup_if_needed(dst_p: str):
        """Must be called before writing or deleting dst_p. Records enough to undo it."""
        if dst_p in prev_installed:
            # One of our own files from the previous version: keep a copy for this transaction.
            if dst_p not in tx_backed_up and os.path.exists(dst_p):
                _copy_atomic(dst_p, os.path.join(tx_backup_dir, os.path.relpath(dst_p, retail_dir)))
                tx_backed_up.add(dst_p)
            return
        if dst_p in backed_up or dst_p in created:
            return
        if not os.path.exists(dst_p):
            # Nothing here yet: remember that this run is the one creating it.
            created.add(dst_p)
            journal_add('created', dst_p)
            return
        # A file that is not ours: save the original before it is overwritten.
        bak_p = os.path.join(backup_dir, os.path.relpath(dst_p, retail_dir))
        if not os.path.exists(bak_p):
            _copy_atomic(dst_p, bak_p)
        backed_up.add(dst_p)
        newly_backed_up.add(dst_p)
        journal_add('backed_up', dst_p)

    def atomic_write_bytes(dst_p: str, data: bytes):
        os.makedirs(os.path.dirname(dst_p), exist_ok=True)
        tmp_p = dst_p + '.ja_tmp'
        try:
            with open(tmp_p, 'wb') as f:
                f.write(data)
            os.replace(tmp_p, dst_p)
        except Exception:
            if os.path.exists(tmp_p):
                try:
                    os.remove(tmp_p)
                except OSError:
                    pass
            raise

    def atomic_copy(src_p: str, dst_p: str):
        os.makedirs(os.path.dirname(dst_p), exist_ok=True)
        tmp_p = dst_p + '.ja_tmp'
        try:
            shutil.copy2(src_p, tmp_p)
            os.replace(tmp_p, dst_p)
        except Exception:
            if os.path.exists(tmp_p):
                try:
                    os.remove(tmp_p)
                except OSError:
                    pass
            raise

    try:
        print(f'[1/4] Enabling Allow Local Files in Registry...')
        set_allow_local_files(1)

        print(f'[2/4] Installing custom Japanese fonts...')
        fonts_src = os.path.join(BASE_DIR, 'fonts')
        for fn in os.listdir(fonts_src):
            if fn.lower().endswith('.ttf'):
                dst = os.path.join(retail_dir, 'fonts', fn)
                backup_if_needed(dst)
                atomic_copy(os.path.join(fonts_src, fn), dst)
                installed.add(dst)

        print(f'[3/4] Reading local CASC data from {game_dir} and applying translations...')
        reader = casc_lib.CascReader(game_dir)
        w3mod = reader.parse_tvfs('vfs-2')
        kokr = reader.parse_tvfs('vfs-85')

        # war3skins.txt font routing
        if 'ui/war3skins.txt' in w3mod:
            raw = reader.read_blte(w3mod['ui/war3skins.txt'][0])
            text = raw.decode('utf-8-sig')
            nl = '\r\n' if '\r\n' in text else '\n'
            routes = {
                'MasterFont:koKR': 'Fonts\\2002.ttf',
                'MasterFontBold:koKR': 'Fonts\\FRIZQT__.TTF',
                'MessageFont:koKR': 'Fonts\\2002.ttf',
                'ChatFont:koKR': 'Fonts\\2002.ttf',
                'EscMenuTextFont:koKR': 'Fonts\\FRIZQT__.TTF',
                'InfoPanelTextFont:koKR': 'Fonts\\2002.ttf',
                'TextTagFont:koKR': 'Fonts\\2002.ttf',
            }
            out_lines = []
            for line in text.split(nl):
                key = line.split('=', 1)[0].strip()
                if key in routes and '=' in line:
                    line = f'{key}={routes[key]}'
                out_lines.append(line)
            dst_s = os.path.join(retail_dir, 'ui', 'war3skins.txt')
            backup_if_needed(dst_s)
            atomic_write_bytes(dst_s, (nl.join(out_lines)).encode('utf-8-sig') if raw.startswith(b'\xef\xbb\xbf') else nl.join(out_lines).encode('utf-8'))
            installed.add(dst_s)

        # cinematicpanel.fdf dialogue font routing
        if 'ui/framedef/ui/cinematicpanel.fdf' in w3mod:
            cin_text = reader.read_blte(w3mod['ui/framedef/ui/cinematicpanel.fdf'][0]).decode('utf-8-sig', 'replace')
            cin_text = cin_text.replace(
                'SetPoint TOPLEFT, "CinematicPortraitCover", TOPRIGHT, 0.015, -0.035,',
                'FrameFont "ChatFont", 0.015, "",\n                SetPoint TOPLEFT, "CinematicPortraitCover", TOPRIGHT, 0.015, -0.035,',
            )
            cin_text = cin_text.replace(
                'FontColor 1.0 1.0 1.0,',
                'FrameFont "ChatFont", 0.015, "",\n                FontColor 1.0 1.0 1.0,',
            )
            dst_cin = os.path.join(retail_dir, 'ui', 'framedef', 'ui', 'cinematicpanel.fdf')
            backup_if_needed(dst_cin)
            atomic_write_bytes(dst_cin, cin_text.encode('utf-8'))
            installed.add(dst_cin)

        modified_campaign_maps = set()
        for rel_path, trans_map in translations.items():
            if not trans_map:
                continue
            if rel_path.startswith('campaign/') and rel_path.endswith(('.w3m', '.w3x')):
                if rel_path not in w3mod:
                    continue
                mpq_bytes = reader.read_blte(w3mod[rel_path][0])
                ko_wts = casc_lib.read_mpq_file(mpq_bytes, '_Locales\\koKR.w3mod\\war3map.wts')
                if not ko_wts:
                    continue
                has_bom = ko_wts.startswith(b'\xef\xbb\xbf')
                new_wts, _ = parsers.rebuild_wts(ko_wts.decode('utf-8-sig', 'replace'), trans_map)
                patched = casc_lib.patch_mpq_file(mpq_bytes, '_Locales\\koKR.w3mod\\war3map.wts', (b'\xef\xbb\xbf' if has_bom else b'') + new_wts.encode('utf-8'))
                j_bytes = casc_lib.read_mpq_file(patched, 'war3map.j')
                if j_bytes:
                    new_j, j_cnt = re.subn(r'(?i)"Campaign([/\\]{1,2})', r'"Campaigns\1', j_bytes.decode('utf-8', 'replace'))
                    if j_cnt > 0:
                        patched = casc_lib.patch_mpq_file(patched, 'war3map.j', new_j.encode('utf-8'))
                for prefix in ('campaign/', 'campaigns/'):
                    dst_m = os.path.join(retail_dir, (prefix + rel_path[len('campaign/'):]).replace('/', os.sep))
                    backup_if_needed(dst_m)
                    atomic_write_bytes(dst_m, patched)
                    installed.add(dst_m)
                modified_campaign_maps.add(rel_path.lower())
            elif rel_path in kokr:
                raw = reader.read_blte(kokr[rel_path][0])
                has_bom = raw.startswith(b'\xef\xbb\xbf')
                ko_text = raw.decode('utf-8-sig', 'replace')
                if rel_path.endswith('.fdf'):
                    new_text, _ = parsers.rebuild_fdf(ko_text, trans_map)
                elif rel_path.endswith('.srt'):
                    new_text, _ = parsers.rebuild_srt(ko_text, trans_map)
                elif rel_path == 'ui/helpstrings.txt':
                    blocks = [b.strip() for b in ko_text.split('\n\n') if b.strip()]
                    for idx in range(len(blocks)):
                        k = f"HELP_BLOCK_{idx+1:03d}"
                        if k in trans_map and trans_map[k] != blocks[idx]:
                            blocks[idx] = trans_map[k]
                    new_text = '\n\n'.join(blocks) + '\n'
                else:
                    new_text, _ = parsers.rebuild_ini_txt(ko_text, trans_map)
                out_bytes = (b'\xef\xbb\xbf' if has_bom else b'') + new_text.encode('utf-8')
                for dst_p in (
                    os.path.join(retail_dir, rel_path.replace('/', os.sep)),
                    os.path.join(retail_dir, '_locales', 'kokr.w3mod', rel_path.replace('/', os.sep)),
                ):
                    backup_if_needed(dst_p)
                    atomic_write_bytes(dst_p, out_bytes)
                    installed.add(dst_p)

        # Route CampaignInfoClassic.txt / CampaignInfoReforged.txt to Campaigns/
        for fn in ('campaigninfoclassic.txt', 'campaigninforeforged.txt'):
            rel_ci = f'ui/{fn}'
            if rel_ci not in w3mod:
                continue
            ci_text = reader.read_blte(w3mod[rel_ci][0]).decode('utf-8-sig', 'replace')
            for m_rel in modified_campaign_maps:
                pattern = re.compile(re.escape(f'"{m_rel}"'), re.IGNORECASE)
                ci_text = pattern.sub(lambda m: '"Campaigns/' + m.group(0)[10:], ci_text)
            dst_ci = os.path.join(retail_dir, 'ui', fn)
            backup_if_needed(dst_ci)
            atomic_write_bytes(dst_ci, ci_text.encode('utf-8'))
            installed.add(dst_ci)

        # Remove stale files from previous version if updating.
        # Each stale file is saved to the transaction backup first, and the user's original
        # is copied (not moved) back, so everything can be undone until the manifest is saved.
        for stale_p in sorted(prev_installed - installed):
            if os.path.exists(stale_p):
                backup_if_needed(stale_p)
                os.remove(stale_p)
            if stale_p in backed_up:
                rel = os.path.relpath(stale_p, retail_dir)
                bak_p = os.path.join(backup_dir, rel)
                if os.path.exists(bak_p):
                    journal_add('stale_baks', bak_p)
                    _copy_atomic(bak_p, stale_p)
                    backed_up.discard(stale_p)

        # Commit point: once this manifest (carrying this run's txid) is saved, the update
        # counts as done, even if the cleanup below is interrupted.
        _write_json_atomic(manifest_path, {
            'game_dir': game_dir,
            'txid': txid,
            'prev_allow_local_files': prev_reg,
            'installed_files': sorted(installed),
            'backed_up_files': sorted(backed_up),
        })

    except Exception:
        rollback_on_error()
        raise

    # Cleanup after commit. If this is interrupted, the next run sees the journal with the
    # same txid as the manifest and simply finishes the cleanup.
    recover_interrupted_install(retail_dir, load_manifest(retail_dir))

    print(f'[4/4] Done! Installed {len(installed)} files to {retail_dir}.')
    print('Make sure Battle.net language for Warcraft III is set to Korean (한국어), then launch the game!')


def uninstall(game_dir: str):
    retail_dir = os.path.join(game_dir, '_retail_')
    backup_dir = os.path.join(retail_dir, '.wc3_ja_backup')
    manifest_path = os.path.join(retail_dir, 'wc3_ja_manifest.json')

    manifest = load_manifest(retail_dir)
    # An install that was cut off is finished or undone first, so that the manifest
    # describes the files on disk.
    recover_interrupted_install(retail_dir, manifest)
    if manifest is None:
        print('No wc3_ja_manifest.json found. Nothing to uninstall.')
        return

    files = manifest.get('installed_files', [])
    backed_up = set(manifest.get('backed_up_files', []))
    removed = 0
    restored = 0
    missing_backups = 0
    # Entries are dropped from these as soon as they are handled, and the manifest is saved
    # before each restore, so an interrupted uninstall can always be resumed.
    remaining_files = set(files)
    remaining_backed_up = set(backed_up)
    missing = set()

    # 'restoring' names the file whose original was being put back when a previous
    # uninstall was cut off. If its backup is gone, the restore itself already happened.
    pending = manifest.get('restoring')
    if isinstance(pending, str) and pending in backed_up:
        bak_p = os.path.join(backup_dir, os.path.relpath(pending, retail_dir))
        if not os.path.exists(bak_p):
            remaining_files.discard(pending)
            remaining_backed_up.discard(pending)
            backed_up.discard(pending)
            restored += 1

    def save_progress(restoring: str | None = None):
        manifest['installed_files'] = sorted(remaining_files)
        manifest['backed_up_files'] = sorted(remaining_backed_up)
        manifest['restoring'] = restoring
        _write_json_atomic(manifest_path, manifest)

    for p in sorted((set(files) | backed_up) & (remaining_files | remaining_backed_up)):
        if p in backed_up:
            bak_p = os.path.join(backup_dir, os.path.relpath(p, retail_dir))
            if not os.path.exists(bak_p):
                print(f'[WARNING] Backup missing for {p}; keeping current file to avoid data loss.')
                missing_backups += 1
                missing.add(p)
                continue
            save_progress(restoring=p)
            # Put the original back in one step (overwrites the installed file).
            os.makedirs(os.path.dirname(p), exist_ok=True)
            os.replace(bak_p, p)
            restored += 1
            remaining_backed_up.discard(p)
            remaining_files.discard(p)
            save_progress()
        else:
            if os.path.exists(p):
                os.remove(p)
                removed += 1
            remaining_files.discard(p)

    if missing_backups == 0:
        if os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
        os.remove(manifest_path)
        restore_allow_local_files(manifest.get('prev_allow_local_files'))
        print(f'Uninstalled {removed} files, restored {restored} backed-up files, and restored Registry setting.')
    else:
        remaining_files &= missing
        remaining_backed_up &= missing
        save_progress()
        print(f'Partially uninstalled {removed} files, restored {restored} backed-up files.')
        print(f'[WARNING] {missing_backups} backup file(s) were missing. wc3_ja_manifest.json was kept so you can retry after restoring backups.')


def main():
    ap = argparse.ArgumentParser(description='Warcraft III: Reforged Japanese Localization Patcher')
    ap.add_argument('action', choices=['install', 'uninstall'])
    ap.add_argument('--game-dir', help='Path to Warcraft III folder (auto-detected if omitted)')
    args = ap.parse_args()

    game_dir = args.game_dir or find_game_dir()
    if not game_dir:
        game_dir = input('Warcraft III フォルダのパスを入力してください (例: C:\\Program Files (x86)\\Warcraft III): ').strip().strip('"')
    if not os.path.isdir(os.path.join(game_dir, 'Data')):
        print(f'ERROR: Warcraft III Data folder not found in: {game_dir}')
        sys.exit(1)

    if args.action == 'install':
        install(game_dir)
    else:
        uninstall(game_dir)


if __name__ == '__main__':
    main()
