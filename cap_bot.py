#!/usr/bin/env python3
"""
Cap Bot — Telegram бот для проверки кап байерами
Флоу:
  1. /start → бот спрашивает ник Notion + группу Keitaro (сохраняет)
  2. 🔍 Проверить капы → бот делает сверку и показывает статус каждого потока
  3. Inline-кнопки Стоп / Холд → меняет статус в Notion прямо из бота
"""

import os, re, json, time, logging, requests, sys
from datetime import datetime, timedelta

# ─── КОНФИГ ───────────────────────────────────────────────────────────────────
def require_env(name):
    val = os.environ.get(name)
    if not val:
        print(f"❌ Переменная окружения {name} не задана!")
        sys.exit(1)
    return val

KEITARO_URL   = require_env("KEITARO_URL")    # https://твой-домен (без слеша)
KEITARO_KEY   = require_env("KEITARO_KEY")    # API ключ Keitaro
NOTION_TOKEN  = require_env("NOTION_TOKEN")   # Notion integration token
NOTION_DB_ID  = require_env("NOTION_DB_ID")   # ID базы Link NEW
TG_BOT_TOKEN  = require_env("TG_BOT_TOKEN")   # Telegram bot token
# ──────────────────────────────────────────────────────────────────────────────

USERS_FILE   = "cap_bot_users.json"    # сохранённые профили байеров
NO_TRAFFIC_DAYS = 7                    # дней без расхода = "не льётся"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("cap_bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

# ─── ХРАНИЛИЩЕ ПОЛЬЗОВАТЕЛЕЙ ──────────────────────────────────────────────────
def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_users(users: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)

# ─── NOTION ───────────────────────────────────────────────────────────────────
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

def find_notion_user(query: str) -> dict | None:
    """Ищет пользователя Notion по имени или email"""
    url = "https://api.notion.com/v1/users"
    r = requests.get(url, headers=NOTION_HEADERS, timeout=15)
    r.raise_for_status()
    users = r.json().get("results", [])
    q = query.lower().strip()
    for u in users:
        name  = (u.get("name") or "").lower()
        email = (u.get("person", {}).get("email") or "").lower()
        if q in name or q in email or name in q:
            return u
    return None

def get_streams_for_buyer(buyer_notion_id: str) -> list[dict]:
    """Возвращает потоки байера со статусами Запущен / Не запущен / Холд"""
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    payload = {
        "filter": {
            "and": [
                {
                    "or": [
                        {"property": "Баер статус", "select": {"equals": "Запущен"}},
                        {"property": "Баер статус", "select": {"equals": "Не запущен"}},
                        {"property": "Баер статус", "select": {"equals": "Холд"}},
                    ]
                },
                {"property": "Ответственный", "people": {"contains": buyer_notion_id}}
            ]
        }
    }
    streams = []
    while True:
        r = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        for page in data["results"]:
            props = page["properties"]
            ln_id = ""
            for key in ["userDefined:ID", "ID", ""]:
                p = props.get(key, {})
                if p.get("type") == "title" and p.get("title"):
                    ln_id = p["title"][0]["plain_text"].strip()
                    break
            if not re.match(r"^LN-\d+$", ln_id):
                continue
            cap_raw = ""
            cap_prop = props.get("Cap", {})
            if cap_prop.get("type") == "rich_text" and cap_prop.get("rich_text"):
                cap_raw = cap_prop["rich_text"][0]["plain_text"]
            cap_num = parse_cap(cap_raw)
            if not cap_num:
                continue
            status = ""
            st_prop = props.get("Баер статус", {})
            if st_prop.get("select"):
                status = st_prop["select"]["name"]
            streams.append({
                "notion_id":  page["id"],
                "ln_id":      ln_id,
                "cap":        cap_num,
                "status":     status,
                "notion_url": page["url"],
            })
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return streams

def parse_cap(raw: str) -> int:
    nums = re.findall(r"\d+", raw)
    return int(nums[-1]) if nums else 0

def set_notion_status(page_id: str, status: str):
    """Меняет Баер статус страницы в Notion"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {"properties": {"Баер статус": {"select": {"name": status}}}}
    r = requests.patch(url, headers=NOTION_HEADERS, json=payload, timeout=15)
    r.raise_for_status()

# ─── KEITARO ──────────────────────────────────────────────────────────────────
def get_group_id(group_name: str) -> str | None:
    headers = {"Api-Key": KEITARO_KEY}
    r = requests.get(f"{KEITARO_URL}/admin_api/v1/groups", headers=headers, timeout=15)
    r.raise_for_status()
    for g in r.json():
        if g.get("name", "").lower() == group_name.lower():
            return str(g["id"])
    return None

def get_campaigns_by_group(group_name: str) -> list[dict]:
    headers = {"Api-Key": KEITARO_KEY}
    group_id = get_group_id(group_name)
    r = requests.get(f"{KEITARO_URL}/admin_api/v1/campaigns", headers=headers, timeout=15)
    r.raise_for_status()
    all_camps = r.json()
    if group_id:
        return [c for c in all_camps if str(c.get("group_id", "")) == group_id]
    return all_camps

def get_stats(campaign_ids: list[int], days: int = 90) -> dict[int, dict]:
    if not campaign_ids:
        return {}
    url = f"{KEITARO_URL}/admin_api/v1/report/build"
    headers = {"Api-Key": KEITARO_KEY, "Content-Type": "application/json"}
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to   = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "range": {"from": date_from, "to": date_to, "timezone": "Europe/Kyiv"},
        "filters": [{"name": "campaign_id", "operator": "IN", "expression": [str(i) for i in campaign_ids]}],
        "grouping": ["campaign_id"],
        "metrics": ["sales", "deposits", "cost"],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    result = {}
    for row in r.json().get("rows", []):
        cid = int(row.get("campaign_id", 0))
        result[cid] = {
            "sales":    int(row.get("sales",    0) or 0),
            "deposits": int(row.get("deposits", 0) or 0),
            "cost":     float(row.get("cost",   0) or 0),
        }
    return result

def get_stats_last_7d(campaign_ids: list[int]) -> dict[int, dict]:
    """Статистика за последние 7 дней для проверки активности"""
    return get_stats(campaign_ids, days=NO_TRAFFIC_DAYS)

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def tg_api(method: str, **kwargs) -> dict:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    r = requests.post(url, json=kwargs, timeout=15)
    r.raise_for_status()
    return r.json()

def tg_send(chat_id, text: str, reply_markup=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", **payload)

def tg_edit(chat_id, message_id, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        tg_api("editMessageText", **payload)
    except:
        pass

def answer_callback(callback_query_id: str, text: str = ""):
    tg_api("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

def main_keyboard():
    return {
        "keyboard": [
            [{"text": "🔍 Проверить капы"}],
            [{"text": "⚙️ Изменить настройки"}],
        ],
        "resize_keyboard": True,
    }

# ─── ПРОВЕРКА КАП ─────────────────────────────────────────────────────────────
def check_caps_for_user(buyer_notion_id: str, keitaro_group: str) -> tuple[str, list[dict]]:
    """
    Возвращает (текст отчёта, список потоков требующих действий)
    Каждый элемент actions: {ln_id, notion_id, action: "stop"|"hold"}
    """
    streams = get_streams_for_buyer(buyer_notion_id)
    if not streams:
        return "📭 Потоков не найдено (статус Запущен/Не запущен/Холд)", []

    campaigns = get_campaigns_by_group(keitaro_group)
    ln_to_cids: dict[str, list[int]] = {}
    for c in campaigns:
        m = re.match(r"^(LN-\d+)", c.get("name", ""))
        if m:
            ln = m.group(1)
            ln_to_cids.setdefault(ln, []).append(int(c["id"]))

    all_cids = [cid for s in streams for cid in ln_to_cids.get(s["ln_id"], [])]
    stats_90d = get_stats(all_cids, days=90)
    stats_7d  = get_stats_last_7d(all_cids)

    lines = [f"📊 <b>Отчёт по капам</b> — {keitaro_group}\n"]
    actions = []

    for s in streams:
        ln_id    = s["ln_id"]
        cap      = s["cap"]
        cids     = ln_to_cids.get(ln_id, [])

        if not cids:
            lines.append(f"⚪ <b>{ln_id}</b> — нет в Keitaro")
            continue

        total_90d = sum(stats_90d.get(c, {}).get("sales", 0) + stats_90d.get(c, {}).get("deposits", 0) for c in cids)
        cost_7d   = sum(stats_7d.get(c, {}).get("cost", 0) for c in cids)
        remaining = cap - total_90d
        pct       = total_90d / cap if cap else 0

        notion_link = f'<a href="{s["notion_url"]}">{ln_id}</a>'

        if pct >= 1.0:
            overflow = total_90d - cap
            lines.append(f"🔴 {notion_link} — <b>ПЕРЕЛИВ на {overflow} FD</b> ({total_90d}/{cap}) — нужен стоп!")
            actions.append({"ln_id": ln_id, "notion_id": s["notion_id"], "action": "stop"})

        elif cost_7d == 0:
            lines.append(f"⚠️ {notion_link} — <b>не льётся {NO_TRAFFIC_DAYS} дней</b> (расход 0) → поставить холд?")
            actions.append({"ln_id": ln_id, "notion_id": s["notion_id"], "action": "hold"})

        elif remaining <= 0:
            lines.append(f"🟡 {notion_link} — капа закрыта ({total_90d}/{cap})")

        else:
            lines.append(f"✅ {notion_link} — ещё <b>{remaining} FD</b> ({total_90d}/{cap})")

    text = "\n".join(lines)
    return text, actions

def build_actions_keyboard(actions: list[dict]) -> dict:
    """Строит inline-клавиатуру с кнопками Стоп/Холд для каждого потока"""
    buttons = []
    for a in actions:
        ln_id     = a["ln_id"]
        notion_id = a["notion_id"]
        if a["action"] == "stop":
            buttons.append([{
                "text": f"⛔ Стоп {ln_id}",
                "callback_data": f"stop:{notion_id}:{ln_id}"
            }])
        elif a["action"] == "hold":
            buttons.append([{
                "text": f"❄️ Холд {ln_id}",
                "callback_data": f"hold:{notion_id}:{ln_id}"
            }])
    return {"inline_keyboard": buttons} if buttons else None

# ─── СОСТОЯНИЯ ДИАЛОГА ────────────────────────────────────────────────────────
STATE_IDLE        = "idle"
STATE_ASK_NOTION  = "ask_notion"
STATE_ASK_GROUP   = "ask_group"

# ─── ОБРАБОТКА СООБЩЕНИЙ ──────────────────────────────────────────────────────
def handle_message(msg: dict, users: dict, states: dict):
    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()

    state = states.get(chat_id, STATE_IDLE)

    # /start
    if text == "/start":
        user = users.get(chat_id)
        if user:
            tg_send(chat_id,
                f"👋 С возвращением, <b>{user['notion_name']}</b>!\n"
                f"Группа Keitaro: <b>{user['keitaro_group']}</b>\n\n"
                f"Нажми кнопку ниже чтобы проверить капы.",
                reply_markup=main_keyboard()
            )
        else:
            states[chat_id] = STATE_ASK_NOTION
            tg_send(chat_id,
                "👋 Привет! Я бот для проверки кап.\n\n"
                "Сначала настроим твой профиль.\n\n"
                "Введи своё имя или email в Notion:"
            )
        return

    # ⚙️ Изменить настройки
    if text == "⚙️ Изменить настройки":
        states[chat_id] = STATE_ASK_NOTION
        tg_send(chat_id, "Введи своё имя или email в Notion:")
        return

    # 🔍 Проверить капы
    if text == "🔍 Проверить капы":
        user = users.get(chat_id)
        if not user:
            states[chat_id] = STATE_ASK_NOTION
            tg_send(chat_id, "Сначала нужно настроить профиль.\n\nВведи своё имя или email в Notion:")
            return

        tg_send(chat_id, "⏳ Загружаю данные из Notion и Keitaro...")

        try:
            report, actions = check_caps_for_user(user["notion_id"], user["keitaro_group"])
            keyboard = build_actions_keyboard(actions)
            tg_send(chat_id, report, reply_markup=keyboard)
        except Exception as e:
            log.error(f"check_caps error: {e}")
            tg_send(chat_id, f"❌ Ошибка при проверке: {e}")
        return

    # Ввод имени Notion
    if state == STATE_ASK_NOTION:
        tg_send(chat_id, "🔍 Ищу тебя в Notion...")
        notion_user = find_notion_user(text)
        if not notion_user:
            tg_send(chat_id,
                f"❌ Пользователь <b>{text}</b> не найден в Notion.\n"
                "Попробуй ввести часть имени или email:"
            )
            return
        # Временно сохраняем notion данные
        states[chat_id] = STATE_ASK_GROUP
        states[f"{chat_id}_notion"] = {
            "id":   notion_user["id"],
            "name": notion_user.get("name", text),
        }
        tg_send(chat_id,
            f"✅ Нашёл: <b>{notion_user.get('name', text)}</b>\n\n"
            f"Теперь введи название своей группы в Keitaro\n"
            f"(например: <code>tetriss_mb</code>):"
        )
        return

    # Ввод группы Keitaro
    if state == STATE_ASK_GROUP:
        notion_data = states.get(f"{chat_id}_notion", {})
        if not notion_data:
            states[chat_id] = STATE_ASK_NOTION
            tg_send(chat_id, "Что-то пошло не так. Введи имя в Notion снова:")
            return

        tg_send(chat_id, f"🔍 Ищу группу <b>{text}</b> в Keitaro...")
        group_id = get_group_id(text)
        if not group_id:
            tg_send(chat_id,
                f"❌ Группа <b>{text}</b> не найдена в Keitaro.\n"
                "Попробуй ещё раз (точное название группы):"
            )
            return

        # Сохраняем профиль
        users[chat_id] = {
            "notion_id":     notion_data["id"],
            "notion_name":   notion_data["name"],
            "keitaro_group": text,
        }
        states[chat_id] = STATE_IDLE
        states.pop(f"{chat_id}_notion", None)
        save_users(users)

        tg_send(chat_id,
            f"✅ Профиль настроен!\n\n"
            f"👤 Notion: <b>{notion_data['name']}</b>\n"
            f"📁 Keitaro группа: <b>{text}</b>\n\n"
            f"Теперь жми <b>🔍 Проверить капы</b>",
            reply_markup=main_keyboard()
        )
        return

    # Неизвестная команда
    tg_send(chat_id, "Используй кнопки ниже 👇", reply_markup=main_keyboard())


def handle_callback(cb: dict, users: dict):
    chat_id          = str(cb["message"]["chat"]["id"])
    message_id       = cb["message"]["message_id"]
    callback_query_id = cb["id"]
    data             = cb.get("data", "")

    parts = data.split(":", 2)
    if len(parts) != 3:
        answer_callback(callback_query_id, "Неизвестное действие")
        return

    action, notion_id, ln_id = parts

    if action == "stop":
        try:
            set_notion_status(notion_id, "Стопнут")
            answer_callback(callback_query_id, f"⛔ {ln_id} → Стопнут")
            # Обновляем сообщение — убираем кнопку
            original_text = cb["message"].get("text", "")
            updated_text  = original_text.replace(
                f"🔴",
                f"⛔ [СТОПНУТ]"
            )
            # Убираем кнопку из клавиатуры
            existing_buttons = cb["message"].get("reply_markup", {}).get("inline_keyboard", [])
            new_buttons = [row for row in existing_buttons
                          if not any(btn.get("callback_data", "").startswith(f"stop:{notion_id}") for btn in row)]
            tg_edit(chat_id, message_id, original_text + f"\n\n✅ <b>{ln_id}</b> → статус изменён на <b>Стопнут</b>",
                    reply_markup={"inline_keyboard": new_buttons} if new_buttons else None)
            log.info(f"Стопнут: {ln_id}")
        except Exception as e:
            answer_callback(callback_query_id, f"Ошибка: {e}")

    elif action == "hold":
        try:
            set_notion_status(notion_id, "Холд")
            answer_callback(callback_query_id, f"❄️ {ln_id} → Холд")
            original_text = cb["message"].get("text", "")
            existing_buttons = cb["message"].get("reply_markup", {}).get("inline_keyboard", [])
            new_buttons = [row for row in existing_buttons
                          if not any(btn.get("callback_data", "").startswith(f"hold:{notion_id}") for btn in row)]
            tg_edit(chat_id, message_id, original_text + f"\n\n✅ <b>{ln_id}</b> → статус изменён на <b>Холд</b>",
                    reply_markup={"inline_keyboard": new_buttons} if new_buttons else None)
            log.info(f"Холд: {ln_id}")
        except Exception as e:
            answer_callback(callback_query_id, f"Ошибка: {e}")


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────
def main():
    log.info("🤖 Cap Bot запущен")
    users   = load_users()
    states  = {}  # chat_id → state
    offset  = 0

    while True:
        try:
            result = tg_api("getUpdates", offset=offset, timeout=20, allowed_updates=["message", "callback_query"])
            updates = result.get("result", [])

            for upd in updates:
                offset = upd["update_id"] + 1

                if "message" in upd:
                    try:
                        handle_message(upd["message"], users, states)
                    except Exception as e:
                        log.error(f"handle_message error: {e}")

                elif "callback_query" in upd:
                    try:
                        handle_callback(upd["callback_query"], users)
                    except Exception as e:
                        log.error(f"handle_callback error: {e}")

        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log.error(f"polling error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
