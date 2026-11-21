const socket = io();
const $ = (id) => document.getElementById(id);
const screens = ["homeScreen","lobbyScreen","roleScreen","discussionScreen","votingScreen","guessScreen","resultScreen","statsScreen"];
let playerId = localStorage.getItem("ww_player_id");
if (!playerId) {
  playerId = (crypto.randomUUID ? crypto.randomUUID() : "guest-" + Math.random().toString(36).slice(2) + Date.now());
  localStorage.setItem("ww_player_id", playerId);
}
let playerName = localStorage.getItem("ww_player_name") || "";
let roomCode = null, myRole = null, myWord = null, isHost = false, latestState = null;
let timerInterval = null, lastResult = null, previousScreen = "homeScreen";

function showScreen(id) {
  screens.forEach(s => $(s).classList.toggle("active", s === id));
}
function toast(message) {
  const el = $("toast"); el.textContent = message; el.classList.add("show");
  clearTimeout(toast._timer); toast._timer = setTimeout(() => el.classList.remove("show"), 2600);
}
function identity() {
  if (!playerName) {
    const entered = prompt("ゲームで使用する名前を入力してください（12文字以内）", "ゲスト");
    playerName = (entered || "ゲスト").trim().slice(0,12) || "ゲスト";
    localStorage.setItem("ww_player_name", playerName);
  }
  return {player_id: playerId, name: playerName};
}
function setConnection(ok) {
  $("connection").innerHTML = `<i style="background:${ok ? '#54d6a0' : '#ff6689'}"></i>${ok ? "接続中" : "再接続中"}`;
}
function updateLobby(state) {
  $("roomCode").textContent = state.code;
  $("playerCount").textContent = state.players.length;
  $("playerList").innerHTML = state.players.map(p => `
    <div class="player-item">
      <div class="avatar">${escapeHtml(p.name.slice(0,1))}</div>
      <span class="player-name">${escapeHtml(p.name)}</span>
      ${p.is_host ? '<span class="host-tag">HOST</span>' : ''}
      <span class="status-dot" title="${p.connected ? '接続中' : '切断'}" style="background:${p.connected ? '#54d6a0' : '#e7a34c'}"></span>
    </div>`).join("");
  isHost = state.host_id === playerId;
  const canStart = isHost && state.players.length >= 3 && state.phase === "WAITING";
  $("startGameButton").disabled = !canStart;
  $("startGameButton").textContent = isHost ? (state.players.length >= 3 ? "ゲーム開始" : "ゲーム開始（3人以上）") : "ホストが開始するのを待っています";
  $("talkSeconds").disabled = !isHost;
  $("category").disabled = !isHost;
  $("hostNote").textContent = isHost ? "あなたはホストです。設定を決めてゲームを開始してください。" : `${state.host_name} さんがホストです。`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function renderDiscussion(state) {
  $("discussionPlayers").innerHTML = state.players.map(p => `<span class="player-chip">${escapeHtml(p.name)}</span>`).join("");
  if (timerInterval) clearInterval(timerInterval);
  const update = () => {
    const left = Math.max(0, Math.ceil((state.deadline * 1000 - Date.now()) / 1000));
    $("timerValue").textContent = left;
    $("timerRing").classList.toggle("warning", left <= 10);
    if (left <= 0 && timerInterval) { clearInterval(timerInterval); timerInterval = null; }
  };
  update(); timerInterval = setInterval(update, 250);
}
function renderVoting(state) {
  const isTie = state.phase === "TIE_VOTING";
  $("votingTitle").textContent = isTie ? "決選投票" : "人狼だと思う人に投票";
  $("votingDescription").textContent = isTie ? "最多得票だったプレイヤーから選んでください。" : "投票は匿名です。投票後の変更はできません。";
  $("voteRound").textContent = state.round_label;
  $("voteProgress").textContent = `${state.vote_count} / ${state.required_votes} 人 投票済み`;
  const eligible = state.eligible_ids || state.players.map(p => p.player_id);
  const alreadyVoted = state.players.find(p => p.player_id === playerId)?.voted;
  $("voteGrid").innerHTML = state.players.filter(p => p.player_id !== playerId && eligible.includes(p.player_id)).map(p => `
    <button class="vote-card" data-target="${p.player_id}" ${alreadyVoted ? "disabled" : ""}>
      <span class="avatar">${escapeHtml(p.name.slice(0,1))}</span>
      <strong>${escapeHtml(p.name)}</strong>
      <small>${alreadyVoted ? "投票済み" : "この人に投票"}</small>
    </button>`).join("");
  document.querySelectorAll(".vote-card").forEach(btn => btn.addEventListener("click", () => {
    if (confirm("このプレイヤーに投票しますか？")) socket.emit("submit_vote", {target_id: btn.dataset.target});
  }));
  if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
}
function renderResult(result) {
  lastResult = result;
  const wolfWin = result.winner === "WOLF";
  $("resultHero").className = `result-hero ${wolfWin ? "wolf-win" : "citizen-win"}`;
  $("resultTitle").textContent = result.winner_label;
  $("resultReason").textContent = result.reason;
  $("resultPlayers").innerHTML = result.players.map(p => `
    <div class="result-player"><span>${escapeHtml(p.name)}</span><span class="role-pill ${p.role === "WOLF" ? "wolf" : ""}">${p.role === "WOLF" ? "人狼" : "市民"}${p.won ? "・勝利" : ""}</span></div>`).join("");
  $("resultVotes").innerHTML = result.votes.length ? result.votes.map(v => `<div class="vote-record"><span>${escapeHtml(v.voter)}</span><span>→ ${escapeHtml(v.target)}</span></div>`).join("") : '<div class="muted">投票記録はありません。</div>';
  $("citizenWord").textContent = result.citizen_word;
  $("wolfWord").textContent = result.wolf_word;
  $("rematchButton").disabled = !isHost;
  $("rematchNote").textContent = isHost ? "ホストとして再戦を開始できます。" : "ホストが再戦を開始するのを待ってください。";
}
function handleState(state) {
  latestState = state;
  if (state.phase === "WAITING") { updateLobby(state); showScreen("lobbyScreen"); }
  else if (state.phase === "ROLE_REVEAL") {
    showScreen("roleScreen");
    $("readyButton").disabled = false;
  } else if (state.phase === "DISCUSSION") { showScreen("discussionScreen"); renderDiscussion(state); }
  else if (state.phase === "VOTING" || state.phase === "TIE_VOTING") { showScreen("votingScreen"); renderVoting(state); }
  else if (state.phase === "WOLF_GUESS") {
    showScreen(myRole === "WOLF" ? "guessScreen" : "discussionScreen");
    if (myRole !== "WOLF") $("discussionPlayers").innerHTML = '<span class="player-chip">人狼が最終回答を考えています...</span>';
  } else if (state.phase === "FINISHED" && state.result) { renderResult(state.result); showScreen("resultScreen"); }
  if (state.phase === "ROLE_REVEAL" && state.ready_ids) {}
}
socket.on("connect", () => setConnection(true));
socket.on("disconnect", () => setConnection(false));
socket.on("name_changed", data => {
    toast(`名前を「${data.name}」に変更しました。`);
});
socket.on("error_message", data => toast(data.message || "エラーが発生しました。"));
socket.on("room_created", data => { roomCode = data.code; toast("ルームを作成しました。"); });
socket.on("joined_room", data => { roomCode = data.code; toast("ルームに参加しました。"); });
socket.on("state", handleState);
socket.on("private_role", data => {
  myRole = data.role; 
  myWord = data.word;

  $("roleCard").className = "role-card";
  $("roleCard").innerHTML = `<div class="role-symbol">✦</div>
    <div class="role-label">YOUR WORD</div>
    <div class="role-word">${escapeHtml(data.word)}</div>`;
  $("roleTip").textContent = "お題を覚えて、会話の中でほかの人との違いを探しましょう。";
  $("readyButton").disabled = false;
});
socket.on("ready_count", data => $("readyStatus").textContent = `準備完了 ${data.ready} / ${data.total} 人`);
socket.on("wolf_guess_prompt", () => { myRole = "WOLF"; showScreen("guessScreen"); });
socket.on("personal_result", data => {
  toast(data.points_earned > 0 ? `勝利！ +${data.points_earned}ポイント` : "今回は敗北。次のゲームで勝とう！");
});

$("changeNameButton").addEventListener("click", () => {
    const entered = prompt(
        "新しい名前を入力してください（12文字以内）",
        playerName || "ゲスト"
    );

    if (entered === null) return;

    const newName = entered.trim().slice(0, 12);
    if (!newName) return toast("名前を入力してください。");

    playerName = newName;
    localStorage.setItem("ww_player_name", playerName);

    if (roomCode) {
        socket.emit("change_name", {name: playerName});
    } else {
        toast(`名前を「${playerName}」に変更しました。`);
    }
});
$("createRoomButton").addEventListener("click", () => socket.emit("create_room", identity()));
$("showJoinButton").addEventListener("click", () => $("joinPanel").classList.toggle("hidden"));
$("joinRoomButton").addEventListener("click", () => {
  const code = $("joinCode").value.trim().toUpperCase();
  if (code.length !== 5) return toast("5文字のルームコードを入力してください。");
  socket.emit("join_room", {...identity(), code});
});
$("joinCode").addEventListener("input", e => e.target.value = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g,"").slice(0,5));
$("copyCodeButton").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(roomCode || $("roomCode").textContent); toast("ルームコードをコピーしました。"); }
  catch { toast("コードを選択してコピーしてください。"); }
});
$("startGameButton").addEventListener("click", () => socket.emit("start_game", {
  talk_seconds: Number($("talkSeconds").value), category: $("category").value
}));
$("readyButton").addEventListener("click", () => {
  $("readyButton").disabled = true; socket.emit("ready");
});
$("submitGuessButton").addEventListener("click", () => {
  const guess = $("wolfGuessInput").value.trim();
  if (!guess) return toast("お題を入力してください。");
  socket.emit("wolf_guess", {guess});
});
$("rematchButton").addEventListener("click", () => socket.emit("rematch"));
$("backLobbyButton").addEventListener("click", () => {
  if (latestState?.phase === "FINISHED") socket.emit("rematch");
});
$("leaveRoomButton").addEventListener("click", () => {
  if (confirm("ルームから退出しますか？")) { socket.emit("leave_room"); roomCode = null; myRole = null; showScreen("homeScreen"); }
});
$("profileButton").addEventListener("click", () => {
  previousScreen = document.querySelector(".screen.active")?.id || "homeScreen";
  socket.emit("get_stats", {player_id: playerId}); socket.emit("get_ranking"); showScreen("statsScreen");
});
$("closeStatsButton").addEventListener("click", () => showScreen(previousScreen));
$("brandHome").addEventListener("click", e => { e.preventDefault(); if (!roomCode) showScreen("homeScreen"); else showScreen("lobbyScreen"); });
socket.on("stats_data", data => {
  const cards = [
    ["対戦数",data.games],["勝利数",data.wins],["勝率",`${data.win_rate}%`],["ポイント",data.points],
    ["市民勝率",`${data.citizen_rate}%`],["人狼勝率",`${data.wolf_rate}%`]
  ];
  $("statsGrid").innerHTML = cards.map(c => `<div class="stat-card"><strong>${c[1]}</strong><span>${c[0]}</span></div>`).join("");
});
socket.on("ranking_data", rows => {
  $("rankingList").innerHTML = rows.length ? rows.map((r,i) => `<div class="rank-row"><span class="rank-no">#${i+1}</span><strong>${escapeHtml(r.display_name)}</strong><span class="rank-points">${r.points} pt</span><span class="rank-rate">${r.win_rate}%</span></div>`).join("") : '<p class="muted">まだランキングデータがありません。</p>';
});
