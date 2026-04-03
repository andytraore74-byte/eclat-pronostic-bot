import os
import logging
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Configuration ──────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8590894588:AAHBDr4q2rHp0gi_JWxT6ID5iEyh55rISMc")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "04c75c996e7842c4bb2d96618adc1db4")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "7645822721"))
FOOTBALL_API_BASE = "https://api.football-data.org/v4"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Keep-alive server ───────────────────────────────────────────
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, format, *args):
        pass

def run_keep_alive():
    server = HTTPServer(("0.0.0.0", 8000), KeepAliveHandler)
    server.serve_forever()

def start_keep_alive():
    t = threading.Thread(target=run_keep_alive, daemon=True)
    t.start()

# ── Auth check ──────────────────────────────────────────────────
def is_authorized(user_id: int) -> bool:
    return user_id == AUTHORIZED_USER_ID

# ── Football API helpers ────────────────────────────────────────
def api_get(endpoint: str, params: dict = None):
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    try:
        r = requests.get(f"{FOOTBALL_API_BASE}{endpoint}", headers=headers, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API error {endpoint}: {e}")
        return None

def get_today_matches():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    data = api_get("/matches", {"dateFrom": today, "dateTo": today, "status": "SCHEDULED"})
    if not data:
        return []
    return data.get("matches", [])

def get_week_matches():
    today = datetime.utcnow()
    end = today + timedelta(days=7)
    data = api_get("/matches", {
        "dateFrom": today.strftime("%Y-%m-%d"),
        "dateTo": end.strftime("%Y-%m-%d"),
        "status": "SCHEDULED"
    })
    if not data:
        return []
    return data.get("matches", [])

def get_team_last_matches(team_id: int, limit: int = 8):
    data = api_get(f"/teams/{team_id}/matches", {"status": "FINISHED", "limit": limit})
    if not data:
        return []
    return data.get("matches", [])

def get_head_to_head(match_id: int):
    data = api_get(f"/matches/{match_id}/head2head", {"limit": 10})
    if not data:
        return []
    return data.get("matches", [])

# ── Analysis engine ─────────────────────────────────────────────
def analyze_team_form(matches: list, team_id: int):
    """Retourne wins, draws, losses, goals_for, goals_against sur les derniers matchs."""
    wins = draws = losses = gf = ga = 0
    for m in matches:
        home_id = m["homeTeam"]["id"]
        away_id = m["awayTeam"]["id"]
        score = m.get("score", {}).get("fullTime", {})
        home_goals = score.get("home") or 0
        away_goals = score.get("away") or 0
        if team_id == home_id:
            gf += home_goals; ga += away_goals
            if home_goals > away_goals: wins += 1
            elif home_goals == away_goals: draws += 1
            else: losses += 1
        elif team_id == away_id:
            gf += away_goals; ga += home_goals
            if away_goals > home_goals: wins += 1
            elif away_goals == home_goals: draws += 1
            else: losses += 1
    return wins, draws, losses, gf, ga

def analyze_h2h(h2h_matches: list, home_id: int, away_id: int):
    home_wins = away_wins = draws = 0
    for m in h2h_matches:
        score = m.get("score", {}).get("fullTime", {})
        hg = score.get("home") or 0
        ag = score.get("away") or 0
        mid_home = m["homeTeam"]["id"]
        mid_away = m["awayTeam"]["id"]
        if mid_home == home_id:
            if hg > ag: home_wins += 1
            elif hg == ag: draws += 1
            else: away_wins += 1
        else:
            if ag > hg: home_wins += 1
            elif ag == hg: draws += 1
            else: away_wins += 1
    return home_wins, draws, away_wins

def compute_predictions(home_form, away_form, h2h_home, h2h_draws, h2h_away):
    hw, hd, hl, hgf, hga = home_form
    aw, ad, al, agf, aga = away_form
    total_h = hw + hd + hl or 1
    total_a = aw + ad + al or 1
    total_h2h = h2h_home + h2h_draws + h2h_away or 1

    # Scores de forme (0-1)
    home_form_score = (hw / total_h * 0.5 + (hgf - hga) / (total_h * 3 + 1) * 0.3 +
                       h2h_home / total_h2h * 0.2)
    away_form_score = (aw / total_a * 0.5 + (agf - aga) / (total_a * 3 + 1) * 0.3 +
                       h2h_away / total_h2h * 0.2)

    total_score = home_form_score + away_form_score + 0.001
    raw_home = home_form_score / total_score
    raw_away = away_form_score / total_score
    raw_draw = 1 - raw_home - raw_away
    raw_draw = max(0.05, raw_draw)

    # Normalise
    s = raw_home + raw_draw + raw_away
    p_home = raw_home / s
    p_draw = raw_draw / s
    p_away = raw_away / s

    # Paris dérivés
    p_btts = min(0.95, (hgf / total_h) * (agf / total_a) * 2.5)
    avg_goals = (hgf / total_h + agf / total_a)
    p_over25 = min(0.95, avg_goals / 3.5)
    p_under25 = 1 - p_over25
    p_home_or_draw = p_home + p_draw
    p_away_or_draw = p_away + p_draw

    predictions = {
        "🏠 Victoire domicile": p_home,
        "🤝 Match nul": p_draw,
        "✈️ Victoire extérieur": p_away,
        "⚽ Les deux équipes marquent (BTTS)": p_btts,
        "📈 Plus de 2.5 buts": p_over25,
        "📉 Moins de 2.5 buts": p_under25,
        "🔒 Domicile ou Nul (1X)": p_home_or_draw,
        "🔒 Extérieur ou Nul (X2)": p_away_or_draw,
    }
    return {k: round(v * 100, 1) for k, v in predictions.items()}

# ── Format helpers ──────────────────────────────────────────────
def format_match_line(m):
    home = m["homeTeam"]["name"]
    away = m["awayTeam"]["name"]
    utc_date = m.get("utcDate", "")
    try:
        dt = datetime.strptime(utc_date, "%Y-%m-%dT%H:%M:%SZ")
        time_str = dt.strftime("%d/%m %H:%M")
    except:
        time_str = "?"
    comp = m.get("competition", {}).get("name", "")
    return f"⚽ {home} vs {away}\n🏆 {comp} | 🕐 {time_str}"

def build_predictions_text(match, predictions):
    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]
    high = {k: v for k, v in predictions.items() if v >= 85}
    lines = [f"📊 *Analyse : {home} vs {away}*\n"]
    if high:
        lines.append("✅ *Prédictions > 85% de confiance :*")
        for k, v in sorted(high.items(), key=lambda x: -x[1]):
            lines.append(f"  {k} : *{v}%*")
    else:
        lines.append("⚠️ Aucune prédiction ne dépasse 85% de confiance pour ce match.")
        lines.append("\n📉 *Meilleures options disponibles :*")
        top3 = sorted(predictions.items(), key=lambda x: -x[1])[:3]
        for k, v in top3:
            lines.append(f"  {k} : {v}%")
    return "\n".join(lines)

# ── Main menu ───────────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Matchs du jour", callback_data="today")],
        [InlineKeyboardButton("📆 Matchs de la semaine", callback_data="week")],
        [InlineKeyboardButton("🔍 Analyser un match", callback_data="analyze_menu")],
        [InlineKeyboardButton("🔄 Redémarrer", callback_data="wake")],
    ])

# ── Handlers ────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return
    await update.message.reply_text(
        "🤖 *Football Predict Bot*\nChoisissez une option :",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

async def wake_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return
    await update.message.reply_text("✅ Bot relancé !", reply_markup=main_menu_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_authorized(user_id):
        await query.edit_message_text("⛔ Accès non autorisé.")
        return

    data = query.data

    if data == "wake":
        await query.edit_message_text("✅ Bot relancé !", reply_markup=main_menu_keyboard())
        return

    if data == "main_menu":
        await query.edit_message_text(
            "🤖 *Football Predict Bot*\nChoisissez une option :",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return

    if data == "today":
        await query.edit_message_text("⏳ Récupération des matchs du jour...")
        matches = get_today_matches()
        if not matches:
            await query.edit_message_text(
                "😴 Aucun match programmé aujourd'hui.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
            )
            return
        buttons = []
        context.user_data["matches"] = {str(m["id"]): m for m in matches[:20]}
        for m in matches[:20]:
            home = m["homeTeam"]["name"][:15]
            away = m["awayTeam"]["name"][:15]
            buttons.append([InlineKeyboardButton(f"{home} vs {away}", callback_data=f"predict_{m['id']}")])
        buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="main_menu")])
        await query.edit_message_text(
            f"📅 *{len(matches)} matchs aujourd'hui — choisissez-en un :*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "week":
        await query.edit_message_text("⏳ Récupération des matchs de la semaine...")
        matches = get_week_matches()
        if not matches:
            await query.edit_message_text(
                "😴 Aucun match programmé cette semaine.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
            )
            return
        context.user_data["matches"] = {str(m["id"]): m for m in matches[:30]}
        buttons = []
        for m in matches[:30]:
            home = m["homeTeam"]["name"][:14]
            away = m["awayTeam"]["name"][:14]
            try:
                dt = datetime.strptime(m["utcDate"], "%Y-%m-%dT%H:%M:%SZ")
                label = f"{dt.strftime('%d/%m')} {home} vs {away}"
            except:
                label = f"{home} vs {away}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"predict_{m['id']}")])
        buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="main_menu")])
        await query.edit_message_text(
            f"📆 *{len(matches)} matchs cette semaine :*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "analyze_menu":
        await query.edit_message_text(
            "🔍 Envoyez-moi le nom de l'équipe à analyser (ex: *Arsenal*)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
        )
        context.user_data["waiting_team"] = True
        return

    if data.startswith("predict_"):
        match_id = data.split("_")[1]
        stored = context.user_data.get("matches", {})
        match = stored.get(match_id)
        if not match:
            await query.edit_message_text("❌ Match introuvable, revenez au menu principal.")
            return
        await query.edit_message_text("⏳ Analyse en cours...")
        home_id = match["homeTeam"]["id"]
        away_id = match["awayTeam"]["id"]
        home_matches = get_team_last_matches(home_id)
        away_matches = get_team_last_matches(away_id)
        h2h = get_head_to_head(int(match_id))
        home_form = analyze_team_form(home_matches, home_id)
        away_form = analyze_team_form(away_matches, away_id)
        h2h_home, h2h_draw, h2h_away = analyze_h2h(h2h, home_id, away_id)
        predictions = compute_predictions(home_form, away_form, h2h_home, h2h_draw, h2h_away)
        text = build_predictions_text(match, predictions)
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
        )
        return

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    if context.user_data.get("waiting_team"):
        context.user_data["waiting_team"] = False
        team_name = update.message.text.strip()
        await update.message.reply_text(f"⏳ Recherche de {team_name}...")
        data = api_get("/teams", {"name": team_name})
        if not data or not data.get("teams"):
            await update.message.reply_text(
                "❌ Équipe introuvable. Essayez avec un nom en anglais (ex: Paris Saint-Germain).",
                reply_markup=main_menu_keyboard()
            )
            return
        teams = data["teams"][:5]
        buttons = [[InlineKeyboardButton(t["name"], callback_data=f"teaminfo_{t['id']}")] for t in teams]
        buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="main_menu")])
        await update.message.reply_text(
            "Choisissez l'équipe :",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

# ── Heartbeat ───────────────────────────────────────────────────
def heartbeat():
    while True:
        time.sleep(120)  # toutes les 2 minutes
        try:
            requests.get("http://localhost:8000", timeout=5)
            logger.info("💓 Heartbeat OK")
        except:
            pass

# ── Main ────────────────────────────────────────────────────────
def main():
    start_keep_alive()
    threading.Thread(target=heartbeat, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wake", wake_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("🚀 Bot démarré !")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
