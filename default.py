# -*- coding: utf-8 -*-
# ライブラリ更新専用アドオン (lib.guiupdate)
#
# 目的: Kodi を DLNA サーバーとして使う場合、音楽ライブラリの
# 更新は GUI 操作でないと反映されない（Kodi 側の挙動。仕様か
# バグかは未確定）ため、登録した対象フォルダへ GUI 操作で
# スキャンを実行する。
#
# 設計（汎用キーマクロ版 lib.uodate からの変更点）:
#   - キーマクロの登録・編集機能は廃止した。実行列は、検証用
#     Windows PC と運用 Raspberry Pi の両方で動作確認済みの
#     固定列（旧 v1.0.10 の標準マクロと同一）だけを使う。
#   - スキャン対象は1つだけ。設定画面のボタンから、登録済みの
#     音楽ソースの一覧（仮想フォルダ）で選ぶ。手入力はしない。
#     （後のスマートプレイリスト自動読み込みサービスとの
#     兼ね合いで、対象は1つに限定する仕様）
#   - 実行中はマウス操作をロックし、終了時に解除する。万一
#     終了処理まで進まなくても、開始から10秒で自動解除する。
#   - 失敗は無言で終わらせず、通知と kodi.log
#     （[lib.guiupdate] プレフィックス）に残す。

import sys
import threading
import time

import xbmc
import xbmcaddon
import xbmcgui

addon = xbmcaddon.Addon()
addon_name = addon.getAddonInfo('name')

DELAY = 1.0               # キー操作の間隔（秒）。実績値（旧 delay=10）
MAX_RUN = 30.0            # 実行全体の上限（秒）。超過したら中断する
MOUSE_AUTORELEASE = 10.0  # マウスロックの自動解除までの秒数

monitor = xbmc.Monitor()
start_time = time.time()


def _log(msg):
    xbmc.log("[lib.guiupdate] " + msg, xbmc.LOGINFO)


def _notify(msg):
    xbmcgui.Dialog().notification(addon_name, msg)


# -------------------------------
# 対象フォルダの選択モード（設定画面の action ボタンから起動）
# RunScript(lib.guiupdate,select_target) の引数が sys.argv[1] に届く。
#
# 登録済みの音楽ソースだけを一覧に出すため、フォルダ参照
# ダイアログ（Dialog().browse）は使わない。browse はメディア
# 種別が分かっていてもローカルドライブを無条件で合成してしまう
# （xbmc/interfaces/legacy/Dialog.cpp の "always append local
# drives"）ため、起点を登録済みソースだけに絞れない。
# 代わりに JSON-RPC の Files.GetSources（media=music）で
# ソースの label / file だけを取得し、Dialog().select で選ぶ。
# import json はこの選択モード内だけの局所 import とし、
# 実行経路では読み込まない（メモリ対策の経緯による）。
# 対象は1つだけなので、選び直せば上書きされる。
# -------------------------------
if len(sys.argv) > 1 and sys.argv[1].strip() == "select_target":
    import json

    _dialog = xbmcgui.Dialog()
    _resp = xbmc.executeJSONRPC(
        '{"jsonrpc":"2.0","method":"Files.GetSources",'
        '"params":{"media":"music"},"id":1}')
    try:
        _sources = json.loads(_resp).get("result", {}).get("sources", [])
    except ValueError:
        _sources = []
    # アドオン用の仮想ソース（addons://）はライブラリフォルダ
    # ではないので除外する。ロックされたソースは API 側で除外済み。
    _sources = [s for s in _sources
                if s.get("file") and not s["file"].startswith("addons://")]
    if not _sources:
        _dialog.notification(addon_name, "登録済みの音楽ソースがありません")
        sys.exit()

    _labels = [s.get("label") or s["file"] for s in _sources]
    _idx = _dialog.select("スキャン対象のソースを選択", _labels)
    if _idx < 0:
        sys.exit()
    _chosen = _sources[_idx]["file"]
    _label = _sources[_idx].get("label") or ""

    _mode = _dialog.select(
        "対象の決め方",
        ["このソース自体を対象にする", "サブフォルダを選ぶ"])
    if _mode == 1:
        # 起点を選んだソースの中に固定して参照する
        _sub = _dialog.browse(0, "サブフォルダを選択", "music",
                               "", False, False, _chosen)
        if not _sub:
            sys.exit()
        _chosen = _sub
        _label = _sub.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
    elif _mode < 0:
        sys.exit()

    addon.setSetting("target_folder", _chosen)
    addon.setSetting("target_label", _label)
    _dialog.notification(addon_name, "対象フォルダを登録しました")
    sys.exit()


# -------------------------------
# マウス操作のロック／解除
# 従来版と同じく JSON-RPC の Settings.SetSettingValue で
# input.enablemouse / input.enablemouseandtouch を切り替える。
# ロック時にタイマーを仕掛け、MOUSE_AUTORELEASE 秒で自動解除
# する（実行がどこかで止まってもマウスが戻らない事態を防ぐ）。
# 解除は冪等で、タイマー側と終了処理側のどちらから呼ばれても
# 二重に実行されない。
# -------------------------------
_mouse_locked = False
_mouse_timer = None


def unlock_mouse():
    global _mouse_locked, _mouse_timer
    if _mouse_timer is not None:
        _mouse_timer.cancel()
        _mouse_timer = None
    if not _mouse_locked:
        return
    _mouse_locked = False
    xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Settings.SetSettingValue","params":{"setting":"input.enablemouse","value":true},"id":1}')
    xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Settings.SetSettingValue","params":{"setting":"input.enablemouseandtouch","value":true},"id":1}')
    _log("mouse unlocked")


def lock_mouse():
    global _mouse_locked, _mouse_timer
    xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Settings.SetSettingValue","params":{"setting":"input.enablemouse","value":false},"id":2}')
    xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Settings.SetSettingValue","params":{"setting":"input.enablemouseandtouch","value":false},"id":2}')
    _mouse_locked = True
    _mouse_timer = threading.Timer(MOUSE_AUTORELEASE, unlock_mouse)
    _mouse_timer.daemon = True
    _mouse_timer.start()
    _log("mouse locked (auto-release in %s sec)" % MOUSE_AUTORELEASE)


# -------------------------------
# 対象フォルダへの直接フォーカス
# 位置決め（FIRSTPAGE,RIGHT のような相対移動）は画面構成の
# 変化で崩れるため、登録済みパスと一致する項目を一覧から探し、
# パスで直接フォーカスする。見つからない／フォーカスできない
# 場合は通知して False を返し、呼び出し側が実行を中断する
# （誤った項目をスキャンしないため）。
# -------------------------------

def _norm_path(p):
    # 比較用に正規化するだけ。元の文字列は変えない。Windowsパス
    # （\）と smb:// 等の / を吸収し、末尾の / と大文字小文字の
    # 違いも吸収する。
    return (p or "").replace("\\", "/").rstrip("/").lower()


def _container_count():
    try:
        return int(xbmc.getInfoLabel("Container.NumItems") or 0)
    except (TypeError, ValueError):
        return 0


def _wait_container(timeout=5.0):
    # 一覧の読み込み完了を固定の待ち任せにせず、項目数が
    # 揃うまで待つ。タイムアウトしても False を返すだけ。
    end = time.time() + timeout
    while time.time() < end:
        if monitor.abortRequested() or time.time() - start_time > MAX_RUN:
            return False
        if _container_count() > 0:
            return True
        time.sleep(0.2)
    return False


def _is_target_focused(target_norm, target_label):
    # フォーカス中の項目が対象かどうかを、実際に画面側の状態で確認する
    p = xbmc.getInfoLabel("ListItem.Path") or ""
    lbl = xbmc.getInfoLabel("ListItem.Label") or ""
    if p and _norm_path(p) == target_norm:
        return True
    if target_label and lbl and lbl == target_label:
        return True
    return False


def _find_target_index(target_norm, target_label):
    # 現在の一覧から対象項目の番号を返す。Path 一致を最優先し、
    # 無ければ Label（ソース名／フォルダ名）一致を探す。
    # 走査内容は kodi.log に残す（実機で合わない場合の切り分け用）。
    label_index = -1
    count = _container_count()
    _log("focus scan start: items=%d" % count)
    for i in range(count):
        p = xbmc.getInfoLabel("Container.ListItemAbsolute(%d).Path" % i)
        lbl = xbmc.getInfoLabel("Container.ListItemAbsolute(%d).Label" % i)
        _log("focus item[%d]: label=%s path=%s" % (i, lbl, p))
        if p and _norm_path(p) == target_norm:
            return i
        if label_index < 0 and target_label and lbl == target_label:
            label_index = i
    return label_index


def _focus_index(index, target_norm, target_label):
    # 第1手段：SetFocus(50, 番号, absolute)。効いたかどうかを
    # 「実際にフォーカスが対象に移ったか」で検証する。
    xbmc.executebuiltin("SetFocus(50,%d,absolute)" % index)
    time.sleep(DELAY)
    if _is_target_focused(target_norm, target_label):
        return True
    # 第2手段：実キー操作で先頭から歩く。
    _log("SetFocus did not land on target; fallback to key walk")
    xbmc.executebuiltin("Action(FirstPage)")
    time.sleep(DELAY)
    for _ in range(index):
        xbmc.executebuiltin("Action(Right)")
        time.sleep(DELAY)
    return _is_target_focused(target_norm, target_label)


def focus_target_folder(target_folder, target_label):
    """target_folder の項目へフォーカスする。成功なら True。

    手順：
      1) sources://music/ を開き、対象がソースそのものならそこで探す。
      2) 見つからなければ対象の親フォルダを開き、そこで探す
         （対象がソース配下のサブフォルダの場合）。
    """
    target_norm = _norm_path(target_folder)

    # 1) ソース一覧で探す
    xbmc.executebuiltin("ActivateWindow(Music,sources://music/)")
    time.sleep(DELAY)
    if _wait_container():
        index = _find_target_index(target_norm, target_label)
        if index >= 0:
            if _focus_index(index, target_norm, target_label):
                return True
            _notify("対象フォルダにフォーカスできませんでした")
            return False

    # 2) 親フォルダで探す（対象がサブフォルダの場合）
    parent = target_norm.rsplit("/", 1)[0] if "/" in target_norm else ""
    if parent and parent != target_norm:
        xbmc.executebuiltin("ActivateWindow(Music,%s,return)" % parent)
        time.sleep(DELAY)
        if _wait_container():
            index = _find_target_index(target_norm, target_label)
            if index >= 0:
                if _focus_index(index, target_norm, target_label):
                    return True
                _notify("対象フォルダにフォーカスできませんでした")
                return False

    _notify("対象フォルダが見つかりません: " + target_folder)
    return False


# -------------------------------
# 本実行：固定の GUI 操作列で対象フォルダをスキャンする
# 操作列は lib.uodate v1.0.10 の標準マクロと同一で、検証用
# Windows PC / 運用 Raspberry Pi の両方で動作確認済み:
#   音楽ファイル一覧を開く → 表示を Wall(500) に →
#   対象へ直接フォーカス → コンテキストメニュー → 上 →
#   決定（スキャン項目）→ 決定（再スキャン確認は「いいえ」=
#   パス限定スキャン）→ 設定画面へ戻る
# -------------------------------

def run():
    global start_time
    target_folder = (addon.getSetting("target_folder") or "").strip()
    target_label = (addon.getSetting("target_label") or "").strip()
    if not target_folder:
        _notify("スキャン対象フォルダが未設定です。アドオン設定から登録してください")
        _log("run aborted: target_folder is empty")
        return

    start_time = time.time()
    lock_mouse()
    try:
        _notify("ライブラリ更新を開始します: " + (target_label or target_folder))
        _log("run start: target=%s label=%s" % (target_folder, target_label))

        # 実行直前のスタック防止策：ESCを送信して1秒待機
        # （従来版で実績のある前処理）
        xbmc.executebuiltin('Action(PreviousMenu)')
        time.sleep(1.0)

        xbmc.executebuiltin('ActivateWindow(Music,sources://music/)')
        time.sleep(DELAY)
        xbmc.executebuiltin('Container.SetViewMode(500)')
        time.sleep(DELAY)

        if not focus_target_folder(target_folder, target_label):
            _log("run aborted: focus failed")
            return

        xbmc.executebuiltin('Action(ContextMenu)')
        time.sleep(DELAY)
        xbmc.executebuiltin('Action(Up)')
        time.sleep(DELAY)
        xbmc.executebuiltin('Action(Select)')
        time.sleep(DELAY)
        xbmc.executebuiltin('Action(Select)')
        time.sleep(DELAY)
        xbmc.executebuiltin('ActivateWindow(Settings)')

        if time.time() - start_time <= MAX_RUN:
            _notify("全処理完了")
        _log("run finished")
    finally:
        # 正常終了・中断・例外のいずれでもマウスを戻す。
        # ここまで到達しない場合も、lock_mouse のタイマーが
        # 10秒で自動解除する。
        unlock_mouse()


run()
