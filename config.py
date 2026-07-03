"""
config.py — Cấu hình trung tâm của bot.
- Chứa token, ID owner/co-owner (hardcode, KHÔNG lộ qua slash command)
- Chứa DEFAULT_CONFIG: mọi threshold mặc định (đều lưu SQLite, chỉnh qua slash command)
"""

import os

# Tự động đọc file .env (nếu có) -> nạp BOT_TOKEN vào biến môi trường.
# Trên VPS đã set sẵn env thật thì vẫn ưu tiên dùng env đó.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============================================================
# 🔑 TOKEN — ưu tiên đọc từ biến môi trường, fallback chuỗi rỗng
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # ĐẶT token trong file .env (BOT_TOKEN=...). KHÔNG hardcode token ở đây!

# ============================================================
# 🔐 OWNER / CO-OWNER — hardcode trực tiếp, KHÔNG có slash command để thêm
# Thêm Discord ID vào danh sách bên dưới
# ============================================================
OWNER_IDS = [
    1141933183546433597  # ← thêm ID owner vào đây
]

CO_OWNER_IDS = [
    1281569606980341762  # ← thêm ID co-owner vào đây
]

# ============================================================
# 📁 ĐƯỜNG DẪN
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "bot.db")
BLACKLIST_FILE = os.path.join(BASE_DIR, "blacklist.txt")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

# ============================================================
# ⚙️ DEFAULT CONFIG — mọi giá trị đều chỉnh được qua slash command
# Key dạng "module_setting", value là số (int) hoặc 0/1 cho toggle
# ============================================================
DEFAULT_CONFIG = {
    # ---------- 🛡️ ANTI-RAID ----------
    "antiraid_enabled": 1,                 # toggle bật/tắt module
    "antiraid_join_threshold": 10,         # số người join...
    "antiraid_join_window": 20,            # ...trong N giây → lockdown
    "antiraid_min_account_age": 86400,     # tuổi account tối thiểu (giây) → kick nếu mới hơn
    "antiraid_captcha_timeout": 600,       # giây chờ verify captcha → kick (10 phút)
    "antiraid_pattern_detect": 1,          # bật detect tên dạng user123/member123 → quarantine
    "antiraid_default_avatar_check": 1,    # default avatar → quarantine (kết hợp tuổi account)
    "antiraid_default_avatar_max_age": 604800,  # CHỈ quarantine default-avatar nếu account < 7 ngày tuổi
    "antiraid_invite_filter": 1,           # xóa invite từ server lạ + warn
    "antiraid_name_similarity": 88,        # % tên giống nhau để gom thành cụm raid
    "antiraid_name_cluster_count": 4,      # số tên giống nhau / window → bật raid mode
    "antiraid_raid_mode_duration": 300,    # raid mode tự tắt sau N giây (5 phút)
    "antiraid_raise_verification": 1,      # raid mode → nâng verification level server lên HIGH

    # ---------- 💣 ANTI-NUKE ----------
    "antinuke_enabled": 1,
    "antinuke_channel_delete_limit": 3,    # xóa N channel...
    "antinuke_channel_delete_window": 10,  # ...trong N giây → revoke quyền
    "antinuke_role_delete_limit": 3,       # xóa N role / window
    "antinuke_role_delete_window": 10,
    "antinuke_create_limit": 5,            # tạo channel/role bất thường N / window
    "antinuke_create_window": 10,
    "antinuke_guild_update_limit": 3,      # đổi tên/avatar server N lần / window
    "antinuke_guild_update_window": 60,
    "antinuke_webhook_limit": 3,           # tạo webhook N / window → chặn
    "antinuke_webhook_window": 60,
    "antinuke_admin_grant_protect": 1,     # cấp admin đột ngột → revoke ngay
    "antinuke_bot_add_protect": 1,         # add bot không whitelist → kick
    "antinuke_backup_interval": 21600,     # backup tự động mỗi 6 giờ (giây)
    "antinuke_auto_restore": 1,            # tự restore sau khi detect nuke

   # ---------- 🔁 ANTI-SPAM ----------
    "antispam_enabled": 1,
    "antispam_dup_limit": 4,               # 4 tin giống nhau...
    "antispam_dup_window": 8,              # ...trong 8 giây -> chặn spam text nhanh
    "antispam_similarity": 80,             # 80% giống nhau mới tính là trùng (bớt oan)
    "antispam_mention_limit": 6,           # tối đa 6 mention / tin
    "antispam_emoji_limit": 15,            # 15 emoji / tin (đếm chuẩn theo grapheme)
    "antispam_caps_percent": 80,           # Giữ 80% để member viết chữ hoa ngắn không bị oan
    "antispam_caps_minlen": 8,             # Check từ 8 ký tự trở lên là hợp lý
    "antispam_slowmode_msgs": 7,           # Có 7 tin nhắn liên tục trong channel...
    "antispam_slowmode_window": 5,         # ...chỉ trong vòng 5 giây -> Ép bật slowmode ngay lập tức
    "antispam_slowmode_duration": 15,      # Slowmode hẳn 15 giây để làm nguội phòng chat
    "antispam_mass_users": 8,              # CHỐNG RAID: 8 người cùng spam 1 nội dung...
    "antispam_mass_window": 15,            # ...trong vòng 15 giây -> Tự động LOCKDOWN kênh đó luôn!
    "antispam_word_repeat": 4,             # 1 từ lặp > 4 lần (ví dụ: "alo alo alo alo") -> Xóa + warn

    # ---------- 🈲 ANTI-CONTENT ----------
    "anticontent_enabled": 1,
    "anticontent_block_cjk": 1,            # block Unicode Trung/Nhật/Hàn
    "anticontent_phishing": 1,             # anti phishing (domain list từ GitHub)
    "anticontent_nitro_scam": 1,           # detect link giả discord/nitro
    "anticontent_invite_block": 1,         # block discord.gg không whitelist
    "anticontent_nsfw": 1,                 # check NSFW (API hoặc keyword)

    # ---------- ⚠️ WARN SYSTEM ----------
    "warn_enabled": 1,
    "warn_expire_days": 30,                # warn tự hết hạn sau N ngày
    "warn_mute1_duration": 600,            # lần 2: mute 10 phút
    "warn_mute2_duration": 3600,           # lần 3: mute 1 giờ
    "warn_purge_messages": 1,              # bị warn/mute → xóa sạch tin gần đây của đối tượng
    "warn_purge_window": 600,              # chỉ xóa tin trong N giây gần nhất (10 phút)

    # ---------- 📋 LOG ----------
    "log_enabled": 1,
    "log_channel_id": 0,                   # ID channel log (0 = chưa set)

    # ---------- 🔐 BẢO VỆ NÂNG CAO ----------
    "ratelimit_mod_commands": 10,          # N lệnh mod / phút / người
    "ratelimit_mod_window": 60,

    # ---------- ROLE IDs (0 = chưa set, bot tự tạo nếu cần) ----------
    "role_unverified": 1511946601105723503,  # role gán khi join, xóa khi verify
    "role_verified": 1491081864327336037,    # role cấp khi verify thành công
    "role_quarantine": 0,                  # role cách ly
    "role_muted": 1512699353800642732,                       # role mute

    # ---------- VERIFY CHANNEL ----------
    "channel_verify": 0,                   # kênh gửi captcha (set qua /setup-verify)
}


def is_owner(user_id: int) -> bool:
    """Check user có phải Owner không."""
    return user_id in OWNER_IDS


def is_co_owner(user_id: int) -> bool:
    """Check user có phải Co-owner không."""
    return user_id in CO_OWNER_IDS


def is_owner_or_coowner(user_id: int) -> bool:
    """Check Owner hoặc Co-owner — dùng cho bypass toàn bộ module."""
    return user_id in OWNER_IDS or user_id in CO_OWNER_IDS


# ✅ Done: config.py — Tiếp theo: database.py
