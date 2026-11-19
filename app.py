from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import sqlite3
import random
import string
import threading
import time
import os
from pathlib import Path

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-in-production")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).with_name("word_werewolf.db"))))
rooms = {}
lock = threading.RLock()

WORD_PAIRS = [
    ("食べ物", "ラーメン", "うどん", "初級"),
    ("食べ物", "カレー", "シチュー", "初級"),
    ("食べ物", "寿司", "刺身", "初級"),
    ("食べ物", "餃子", "焼売", "中級"),
    ("食べ物", "たこ焼き", "お好み焼き", "中級"),
    ("動物", "犬", "猫", "初級"),
    ("動物", "ライオン", "トラ", "初級"),
    ("動物", "イルカ", "クジラ", "中級"),
    ("動物", "ワニ", "カメ", "中級"),
    ("スポーツ", "サッカー", "フットサル", "中級"),
    ("スポーツ", "野球", "ソフトボール", "中級"),
    ("スポーツ", "テニス", "バドミントン", "中級"),
    ("乗り物", "電車", "新幹線", "初級"),
    ("乗り物", "飛行機", "ヘリコプター", "中級"),
    ("日用品", "鉛筆", "シャープペンシル", "初級"),
    ("日用品", "冷蔵庫", "冷凍庫", "中級"),
    ("ゲーム", "マリオ", "ソニック", "中級"),
    ("ゲーム", "ポケモン", "デジモン", "中級"),
    ("場所", "遊園地", "テーマパーク", "中級"),
    ("場所", "海", "プール", "初級"),
    ("学校", "大学", "専門学校", "中級"),
    ("職業", "医者", "看護師", "中級"),
    ("自然", "台風", "竜巻", "上級"),
    ("自然", "火山", "地震", "上級"),
]

def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            player_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            games INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            citizen_games INTEGER NOT NULL DEFAULT 0,
            citizen_wins INTEGER NOT NULL DEFAULT 0,
            wolf_games INTEGER NOT NULL DEFAULT 0,
            wolf_wins INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0
        )
        """)
        con.commit()

def clean_name(value):
    value = str(value or "").strip()
    return value[:12] or "ゲスト"

def valid_identity(data):
    data = data or {}
    player_id = str(data.get("player_id", "")).strip()[:80]
    name = clean_name(data.get("name"))
    if not player_id:
        return None, name
    return player_id, name

def new_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(100):
        code = "".join(random.choice(alphabet) for _ in range(5))
        if code not in rooms:
            return code
    raise RuntimeError("ルームコードを発行できませんでした")

def get_room(code):
    return rooms.get(str(code or "").upper())

def player_by_id(room, player_id):
    return room["players"].get(player_id)

def public_state(room):
    players = []
    for p in room["players"].values():
        players.append({
            "player_id": p["player_id"],
            "name": p["name"],
            "is_host": p["player_id"] == room["host_id"],
            "connected": p["sid"] is not None,
            "voted": p["player_id"] in room["votes"],
        })
    return {
        "code": room["code"],
        "phase": room["phase"],
        "players": players,
        "host_id": room["host_id"],
        "host_name": room["players"].get(room["host_id"], {}).get("name", ""),
        "max_players": 5,
        "talk_seconds": room["talk_seconds"],
        "deadline": room.get("deadline"),
        "category": room.get("category", "ランダム"),
        "round": room.get("round", 1),
        "round_label": room.get("round_label", "通常投票"),
        "eligible_ids": room.get("eligible_ids"),
        "vote_count": len(room["votes"]),
        "required_votes": len(room.get("eligible_ids") or room["players"]),
        "result": room.get("result"),
        "message": room.get("message", ""),
    }

def broadcast(room):
    socketio.emit("state", public_state(room), to=room["code"])

def cancel_timer(room):
    timer = room.get("timer")
    if timer:
        timer.cancel()
        room["timer"] = None

def schedule_timer(room, seconds, callback):
    cancel_timer(room)
    room["deadline"] = time.time() + seconds
    timer = threading.Timer(seconds, lambda: callback(room["code"], room["game_token"]))
    timer.daemon = True
    room["timer"] = timer
    timer.start()

def get_current_room_for_sid(sid):
    for room in rooms.values():
        for p in room["players"].values():
            if p["sid"] == sid:
                return room, p
    return None, None

def emit_error(message):
    emit("error_message", {"message": message})

def ensure_host(room, player):
    return player and player["player_id"] == room["host_id"]

def start_discussion(code, token):
    with lock:
        room = get_room(code)
        if not room or room["game_token"] != token or room["phase"] != "DISCUSSION":
            return
        room["phase"] = "DISCUSSION"
        room["message"] = "トークを開始しました。"
        broadcast(room)

def start_voting(room, tie=False):
    cancel_timer(room)
    room["phase"] = "TIE_VOTING" if tie else "VOTING"
    room["votes"] = {}
    room["round_label"] = "決選投票" if tie else "通常投票"
    room["deadline"] = None
    room["message"] = "怪しいと思うプレイヤーに投票してください。"
    broadcast(room)
    begin_voting_timer(room, room["talk_seconds"])

def discussion_timeout(code, token):
    with lock:
        room = get_room(code)
        if not room or room["game_token"] != token or room["phase"] != "DISCUSSION":
            return
        start_voting(room)

def voting_timeout(code, token):
    with lock:
        room = get_room(code)
        if not room or room["game_token"] != token or room["phase"] not in ("VOTING", "TIE_VOTING"):
            return
        # 未投票者はランダム投票として扱う（放置による進行停止を防ぐ）
        eligible = room.get("eligible_ids") if room["phase"] == "TIE_VOTING" else list(room["players"].keys())
        eligible = eligible or list(room["players"].keys())
        targets = [pid for pid in room["players"] if pid in eligible]
        for voter_id in eligible:
            if voter_id not in room["votes"] and targets:
                choices = [x for x in targets if x != voter_id]
                if choices:
                    room["votes"][voter_id] = random.choice(choices)
        resolve_votes(room)

def begin_voting_timer(room, seconds):
    schedule_timer(room, seconds, voting_timeout)

def save_game_stats(room, winner):
    # winner: "CITIZEN" or "WOLF"; one-time per game
    if room.get("stats_saved"):
        return
    room["stats_saved"] = True
    with db() as con:
        for p in room["players"].values():
            is_wolf = p["role"] == "WOLF"
            won = (winner == "WOLF") if is_wolf else (winner == "CITIZEN")
            points = (3 if is_wolf else 2) if won else 0
            con.execute("""
            INSERT INTO stats(player_id, display_name, games, wins, citizen_games, citizen_wins,
                              wolf_games, wolf_wins, points)
            VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
              display_name=excluded.display_name,
              games=games+1,
              wins=wins+excluded.wins,
              citizen_games=citizen_games+excluded.citizen_games,
              citizen_wins=citizen_wins+excluded.citizen_wins,
              wolf_games=wolf_games+excluded.wolf_games,
              wolf_wins=wolf_wins+excluded.wolf_wins,
              points=points+excluded.points
            """, (
                p["player_id"], p["name"], int(won),
                int(not is_wolf), int((not is_wolf) and won),
                int(is_wolf), int(is_wolf and won), points
            ))
        con.commit()

def finish_game(room, winner, reason):
    cancel_timer(room)
    room["phase"] = "FINISHED"
    room["deadline"] = None
    room["message"] = reason
    room["result"] = {
        "winner": winner,
        "winner_label": "人狼側の勝利" if winner == "WOLF" else "市民側の勝利",
        "reason": reason,
        "wolf_id": next((p["player_id"] for p in room["players"].values() if p["role"] == "WOLF"), None),
        "wolf_name": next((p["name"] for p in room["players"].values() if p["role"] == "WOLF"), ""),
        "citizen_word": room["citizen_word"],
        "wolf_word": room["wolf_word"],
        "votes": [
            {"voter": room["players"][v]["name"], "target": room["players"][t]["name"]}
            for v, t in room["votes"].items() if v in room["players"] and t in room["players"]
        ],
        "players": [
            {"player_id": p["player_id"], "name": p["name"],
             "role": p["role"], "word": p["word"],
             "won": ((winner == "WOLF") if p["role"] == "WOLF" else (winner == "CITIZEN"))}
            for p in room["players"].values()
        ]
    }
    save_game_stats(room, winner)
    broadcast(room)
    for p in room["players"].values():
        if p["sid"]:
            socketio.emit("personal_result", {
                "player_id": p["player_id"],
                "points_earned": (3 if p["role"] == "WOLF" else 2)
                    if ((winner == "WOLF") if p["role"] == "WOLF" else (winner == "CITIZEN")) else 0
            }, to=p["sid"])

def resolve_votes(room):
    cancel_timer(room)
    votes = room["votes"]
    if not votes:
        finish_game(room, "WOLF", "有効な投票がありませんでした。人狼側の勝利です。")
        return
    counts = {}
    for target in votes.values():
        counts[target] = counts.get(target, 0) + 1
    high = max(counts.values())
    leaders = [pid for pid, count in counts.items() if count == high]

    if len(leaders) > 1:
        if room["phase"] == "TIE_VOTING":
            finish_game(room, "WOLF", "決選投票でも同票になりました。人狼側の勝利です。")
            return
        room["eligible_ids"] = leaders
        room["round"] = 2
        room["message"] = "最多得票が同数です。該当者を対象に決選投票を行います。"
        start_voting(room, tie=True)
        return

    eliminated_id = leaders[0]
    eliminated = room["players"].get(eliminated_id)
    if not eliminated:
        finish_game(room, "WOLF", "投票結果を処理できませんでした。")
        return

    room["eliminated_id"] = eliminated_id
    room["eliminated_name"] = eliminated["name"]
    room["message"] = f"{eliminated['name']} さんが追放されました。"
    if eliminated["role"] == "WOLF":
        room["phase"] = "WOLF_GUESS"
        room["deadline"] = None
        room["eligible_ids"] = None
        wolf_sid = next((p["sid"] for p in room["players"].values() if p["role"] == "WOLF"), None)
        broadcast(room)
        if wolf_sid:
            socketio.emit("wolf_guess_prompt", {"message": "市民のお題を入力してください。"}, to=wolf_sid)
    else:
        finish_game(room, "WOLF", f"{eliminated['name']} さんは市民でした。人狼側の勝利です。")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/health")
def health():
    return {"status": "ok"}

@socketio.on("connect")
def on_connect():
    emit("connected", {"sid": request.sid})

@socketio.on("create_room")
def on_create_room(data):
    player_id, name = valid_identity(data)
    if not player_id:
        return emit_error("プレイヤーIDを取得できません。ページを再読み込みしてください。")
    with lock:
        code = new_code()
        room = {
            "code": code, "host_id": player_id, "players": {},
            "phase": "WAITING", "talk_seconds": 60, "category": "ランダム",
            "votes": {}, "timer": None, "deadline": None, "game_token": 0,
            "round": 1, "round_label": "通常投票", "eligible_ids": None,
            "result": None, "stats_saved": False
        }
        room["players"][player_id] = {
            "player_id": player_id, "name": name, "sid": request.sid,
            "role": None, "word": None
        }
        rooms[code] = room
        join_room(code)
        emit("room_created", {"code": code, "player_id": player_id})
        broadcast(room)

@socketio.on("join_room")
def on_join_room(data):
    data = data or {}
    player_id, name = valid_identity(data)
    code = str(data.get("code", "")).strip().upper()
    with lock:
        room = get_room(code)
        if not room:
            return emit_error("ルームが見つかりません。コードを確認してください。")
        if room["phase"] != "WAITING":
            return emit_error("このルームではすでにゲームが始まっています。")
        if player_id in room["players"]:
            room["players"][player_id]["sid"] = request.sid
            room["players"][player_id]["name"] = name
        else:
            if len(room["players"]) >= 5:
                return emit_error("このルームは満員です。")
            room["players"][player_id] = {
                "player_id": player_id, "name": name, "sid": request.sid,
                "role": None, "word": None
            }
        join_room(code)
        emit("joined_room", {"code": code, "player_id": player_id})
        broadcast(room)

@socketio.on("leave_room")
def on_leave_room():
    with lock:
        room, player = get_current_room_for_sid(request.sid)
        if not room or not player:
            return
        code = room["code"]
        pid = player["player_id"]
        leave_room(code)
        del room["players"][pid]
        if not room["players"]:
            cancel_timer(room)
            del rooms[code]
            return
        if room["host_id"] == pid:
            room["host_id"] = next(iter(room["players"]))
        if room["phase"] not in ("WAITING", "FINISHED"):
            finish_game(room, "CITIZEN", f"{player['name']} さんが退出したためゲームを終了しました。")
        else:
            broadcast(room)

@socketio.on("disconnect")
def on_disconnect():
    with lock:
        room, player = get_current_room_for_sid(request.sid)
        if room and player:
            player["sid"] = None
            broadcast(room)

@socketio.on("start_game")
def on_start_game(data):
    data = data or {}
    with lock:
        room, player = get_current_room_for_sid(request.sid)
        if not room:
            return emit_error("ルームに参加していません。")
        if not ensure_host(room, player):
            return emit_error("ゲームを開始できるのはホストだけです。")
        if room["phase"] != "WAITING":
            return emit_error("現在ゲームを開始できません。")
        if not 3 <= len(room["players"]) <= 5:
            return emit_error("ゲーム開始には3〜5人の参加が必要です。")
        seconds = data.get("talk_seconds", 60)
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            seconds = 60
        if not 15 <= seconds <= 300:
            return emit_error("トーク時間は15〜300秒で設定してください。")
        category = str(data.get("category", "ランダム"))
        pairs = [x for x in WORD_PAIRS if category == "ランダム" or x[0] == category]
        if not pairs:
            pairs = WORD_PAIRS
        category_name, citizen_word, wolf_word, difficulty = random.choice(pairs)
        players = list(room["players"].values())
        wolf = random.choice(players)
        room["talk_seconds"] = seconds
        room["category"] = category
        room["citizen_word"] = citizen_word
        room["wolf_word"] = wolf_word
        room["phase"] = "ROLE_REVEAL"
        room["votes"] = {}
        room["round"] = 1
        room["eligible_ids"] = None
        room["result"] = None
        room["stats_saved"] = False
        room["game_token"] += 1
        room["game_id"] = f"{room['code']}-{room['game_token']}"
        for p in players:
            p["role"] = "WOLF" if p["player_id"] == wolf["player_id"] else "CITIZEN"
            p["word"] = wolf_word if p["role"] == "WOLF" else citizen_word
            if p["sid"]:
                socketio.emit("private_role", {
                    "role": p["role"], "word": p["word"],
                    "category": category_name, "difficulty": difficulty
                }, to=p["sid"])
        room["message"] = "お題を確認してください。全員の準備ができたらトークを開始します。"
        room["ready_ids"] = set()
        broadcast(room)

@socketio.on("ready")
def on_ready():
    with lock:
        room, player = get_current_room_for_sid(request.sid)
        if not room or room["phase"] != "ROLE_REVEAL":
            return
        room["ready_ids"].add(player["player_id"])
        socketio.emit("ready_count", {"ready": len(room["ready_ids"]), "total": len(room["players"])}, to=room["code"])
        if len(room["ready_ids"]) == len(room["players"]):
            room["phase"] = "DISCUSSION"
            room["message"] = "全員の準備が完了しました。トーク開始！"
            token = room["game_token"]
            broadcast(room)
            schedule_timer(room, room["talk_seconds"], discussion_timeout)
        else:
            broadcast(room)

@socketio.on("submit_vote")
def on_submit_vote(data):
    data = data or {}
    target_id = str(data.get("target_id", ""))
    with lock:
        room, player = get_current_room_for_sid(request.sid)
        if not room or room["phase"] not in ("VOTING", "TIE_VOTING"):
            return emit_error("現在は投票できません。")
        voter_id = player["player_id"]
        eligible = room.get("eligible_ids") if room["phase"] == "TIE_VOTING" else list(room["players"].keys())
        if voter_id not in eligible:
            return emit_error("今回の投票対象ではありません。")
        if voter_id in room["votes"]:
            return emit_error("すでに投票しています。")
        if target_id not in room["players"] or target_id == voter_id:
            return emit_error("投票先が正しくありません。")
        if room["phase"] == "TIE_VOTING" and target_id not in room["eligible_ids"]:
            return emit_error("決選投票の対象者を選んでください。")
        room["votes"][voter_id] = target_id
        broadcast(room)
        if len(room["votes"]) >= len(eligible):
            resolve_votes(room)

@socketio.on("wolf_guess")
def on_wolf_guess(data):
    data = data or {}
    guess = str(data.get("guess", "")).strip()
    with lock:
        room, player = get_current_room_for_sid(request.sid)
        if not room or room["phase"] != "WOLF_GUESS":
            return emit_error("現在は最終回答できません。")
        if player["role"] != "WOLF":
            return emit_error("最終回答できるのは人狼だけです。")
        if guess == room["citizen_word"]:
            finish_game(room, "WOLF", "人狼が市民のお題を当てました。人狼側の逆転勝利です。")
        else:
            finish_game(room, "CITIZEN", "人狼は市民のお題を当てられませんでした。市民側の勝利です。")

@socketio.on("rematch")
def on_rematch():
    with lock:
        room, player = get_current_room_for_sid(request.sid)
        if not room or room["phase"] != "FINISHED":
            return emit_error("再戦できる状態ではありません。")
        if not ensure_host(room, player):
            return emit_error("再戦を開始できるのはホストだけです。")
        room["phase"] = "WAITING"
        room["votes"] = {}
        room["result"] = None
        room["message"] = "再戦メンバーを待っています。"
        room["round"] = 1
        room["eligible_ids"] = None
        room["deadline"] = None
        for p in room["players"].values():
            p["role"] = None
            p["word"] = None
        broadcast(room)

@socketio.on("get_stats")
def on_get_stats(data):
    data = data or {}
    player_id = str(data.get("player_id", ""))
    with db() as con:
        row = con.execute("SELECT * FROM stats WHERE player_id=?", (player_id,)).fetchone()
    if not row:
        emit("stats_data", {
            "games": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "citizen_games": 0, "citizen_wins": 0, "citizen_rate": 0,
            "wolf_games": 0, "wolf_wins": 0, "wolf_rate": 0, "points": 0
        })
    else:
        d = dict(row)
        d["losses"] = d["games"] - d["wins"]
        d["win_rate"] = round(d["wins"] / d["games"] * 100) if d["games"] else 0
        d["citizen_rate"] = round(d["citizen_wins"] / d["citizen_games"] * 100) if d["citizen_games"] else 0
        d["wolf_rate"] = round(d["wolf_wins"] / d["wolf_games"] * 100) if d["wolf_games"] else 0
        emit("stats_data", d)

@socketio.on("get_ranking")
def on_get_ranking():
    with db() as con:
        rows = con.execute("""
          SELECT display_name, games, wins, points,
                 CASE WHEN games > 0 THEN ROUND(wins * 100.0 / games) ELSE 0 END AS win_rate
          FROM stats ORDER BY points DESC, wins DESC, games ASC LIMIT 20
        """).fetchall()
    emit("ranking_data", [dict(r) for r in rows])

init_db()

if __name__ == "__main__":
    print("Word Werewolf is running at http://127.0.0.1:5000")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, use_reloader=False)
