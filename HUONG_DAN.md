# 🛡️ Discord Security Bot — Hướng Dẫn Toàn Diện

Bot bảo vệ server Discord viết bằng **Python + discord.py + aiosqlite**, gồm 6 module bảo vệ độc lập, hệ thống warn tích lũy, backup tự động, và phân quyền Owner/Co-owner/Whitelist.

---

## 📦 1. CÀI ĐẶT & KHỞI ĐỘNG

### Yêu cầu
- Python 3.10+
- **Thư viện TỰ CÀI** — `main.py` có sẵn auto-install: khởi động sẽ check từng thư viện, thiếu mới cài, đã cài thì bỏ qua. Upload lên host là chạy được luôn, deploy lại cũng tự cài lại.
- Host nào đọc `requirements.txt` (Railway, Heroku, Pterodactyl...) thì tự cài qua file đó; không thì auto-install trong `main.py` lo.

Cài tay (không bắt buộc):
```powershell
pip install -r requirements.txt
```

> ▶️ **File chính để chạy bot là `main.py`** — trên host điền startup file/command: `python main.py`

### Cấu hình bắt buộc trước khi chạy

**1. Token bot** — đặt biến môi trường (khuyến nghị) hoặc sửa trực tiếp `config.py`:
```powershell
$env:BOT_TOKEN = "token-cua-ban"
```

**2. Owner/Co-owner** — mở `config.py`, điền Discord ID:
```python
OWNER_IDS = [1141933183546433597]      # ID của bạn
CO_OWNER_IDS = [1281569606980341762]   # ID phó chủ (nếu có)
```
> ⚠️ Owner/Co-owner **chỉ thêm được bằng cách sửa file này** — không có lệnh slash nào thêm được (chống chiếm quyền).

**3. Intents trên Discord Developer Portal** — bật cả 3:
- ✅ Presence Intent *(không bắt buộc)*
- ✅ **Server Members Intent** (bắt buộc — anti-raid)
- ✅ **Message Content Intent** (bắt buộc — anti-spam/content)

**4. Quyền bot khi invite** — tối thiểu: `Administrator` (khuyến nghị) hoặc: Manage Roles, Manage Channels, Kick, Ban, Manage Messages, Manage Webhooks, **View Audit Log** (bắt buộc cho anti-nuke), Moderate Members.
> ⚠️ Role của bot phải **cao hơn** role của thành viên thường trong danh sách role.

### Chạy bot
```powershell
python main.py
```

### Sau khi bot online — setup lần đầu trong server
```
/setlog #kenh-log              ← BẮT BUỘC để nhận log
/setup-verify #kenh-verify     ← kênh nhận captcha + tự tạo role Verified
/config view                   ← xem toàn bộ cấu hình
/whitelist add @admin1         ← thêm admin tin cậy (chỉ Owner/Co-owner gõ được)
/whitelistbot add <bot_id>     ← whitelist các bot đang dùng (Mee6, Carl...)
```

---

## 🗂️ 2. CẤU TRÚC FILE

```
bot/
├── main.py               # ◄ FILE CHÍNH ĐỂ CHẠY — auto-install thư viện, load cogs, sync slash
├── requirements.txt      # Danh sách thư viện (host tự đọc để cài)
├── config.py             # Token, OWNER_IDS/CO_OWNER_IDS, DEFAULT_CONFIG
├── database.py           # SQLite (aiosqlite) + cache RAM
├── blacklist.txt         # Từ cấm GLOBAL (mỗi dòng 1 từ, # là comment)
├── HUONG_DAN.md          # File này
├── cogs/
│   ├── logger.py         # Log mọi sự kiện vào channel log
│   ├── warn_system.py    # Warn tích lũy + thang phạt + mute persist
│   ├── anti_spam.py      # 8 cơ chế chống spam
│   ├── anti_content.py   # Lọc nội dung: CJK, phishing, scam, invite, NSFW
│   ├── anti_raid.py      # Mass join, account age, captcha, quarantine
│   ├── anti_nuke.py      # Chống phá server + backup 6h + auto restore
│   └── config_commands.py# Mọi lệnh slash cấu hình/quản trị
├── utils/
│   ├── backup.py         # Backup/restore JSON (roles, channels, perms)
│   ├── captcha.py        # Captcha verify (nút + modal nhập mã)
│   └── helpers.py        # Decorator @is_owner_or_coowner, @is_whitelisted
└── data/
    ├── bot.db            # SQLite database
    └── backups/          # File backup JSON (giữ 10 file/guild)
```

---

## 🔐 3. PHÂN QUYỀN (quan trọng nhất)

### 3 cấp quyền

| Cấp | Cách thêm | Quyền |
|---|---|---|
| **Owner / Co-owner** | Sửa `config.py` (hardcode) | Bypass **TOÀN BỘ** module + dùng được mọi lệnh, kể cả `/whitelist`, `/backup`, `/restore` |
| **Whitelist user** | `/whitelist add` (chỉ Owner/Co-owner gõ được) | Bypass toàn bộ module anti-* + warn + rate limit; dùng được `/lockdown` |
| **Mod thường** | Quyền Discord (Administrator, Moderate Members...) | Dùng `/warn`, `/config`, `/lock`... — **vẫn bị anti-nuke giám sát** |

### Cách hoạt động
- **Mọi event anti-\* đều check bypass TRƯỚC KHI xử lý** — Owner/Co-owner/whitelist nhắn gì, xóa gì cũng không bị bot can thiệp.
- **Nhưng mọi hành động vẫn được LOG đầy đủ** để audit — bypass xử phạt ≠ bypass log.
- Decorator trong `utils/helpers.py`:
  - `@is_owner_or_coowner()` — gắn cho lệnh tối nhạy cảm (whitelist, backup, restore)
  - `@is_whitelisted()` — gắn cho lệnh nhạy cảm vừa (lockdown)

### Lệnh whitelist
| Lệnh | Ai dùng được | Tác dụng |
|---|---|---|
| `/whitelist add <user>` | Owner/Co-owner | User bypass all module |
| `/whitelist remove <user>` | Owner/Co-owner | Xóa khỏi whitelist |
| `/whitelist list` | Owner/Co-owner | Xem danh sách (kèm Owner/Co-owner) |
| `/whitelistbot add <bot_id>` | Owner/Co-owner | Bot được phép tồn tại trong server |
| `/whitelistbot remove <bot_id>` | Owner/Co-owner | Bot sẽ bị kick nếu được add lại |

---

## 🛡️ 4. MODULE ANTI-RAID

**Cách hoạt động:** mỗi khi có người join, bot chạy chuỗi check theo thứ tự:

```
Member join
  ├─ 1. Đếm join: ≥10 người/20s? → 🚨 LOCKDOWN toàn server
  ├─ 2. Account < 1 ngày tuổi? → 👢 Kick (kèm DM giải thích)
  ├─ 3. Tên dạng user123/member99/test_5? → 🔒 Quarantine role
  ├─ 4. Không có avatar? → 🔒 Quarantine role
  └─ 5. Gán role Unverified → gửi captcha
       ├─ Verify đúng trong 60s → gỡ Unverified, chào mừng ✅
       └─ Hết 60s chưa verify → 👢 Kick
```

**Captcha:** member mới nhận embed có nút **✅ Verify** → bấm mở popup nhập mã 6 ký tự. Thứ tự gửi:
1. **Kênh verify** (set qua `/setup-verify`) — tin tự xóa sau timeout, không loãng kênh ← khuyến nghị
2. DM (khi chưa setup kênh verify)
3. System channel (fallback cuối)

Không gửi được → quarantine thay vì kick oan. Nút verify khóa đúng người (người khác bấm hộ bị từ chối).

**`/setup-verify <channel> [verified_role]`** (Administrator):
- Đặt kênh nhận captcha; bỏ trống role → bot tự tạo role **Verified** (màu xanh)
- Tự chỉnh permission: Unverified **thấy** kênh verify (không gửi tin được), Verified **ẩn** kênh (verify xong khỏi thấy nữa)
- Verify thành công: gỡ Unverified + **cấp Verified role**

**Role tự tạo:** `Unverified`, `Verified`, `Quarantine`, `Muted` — bot tự tạo lần đầu + chặn quyền send/speak ở mọi channel (riêng Verified không chặn gì), ID lưu vào config.

| Config key | Mặc định | Ý nghĩa |
|---|---|---|
| `antiraid_join_threshold` / `antiraid_join_window` | 10 / 20 | N người join trong N giây → lockdown |
| `antiraid_min_account_age` | 86400 | Tuổi account tối thiểu (giây) |
| `antiraid_captcha_timeout` | 60 | Giây chờ verify |
| `antiraid_pattern_detect` | 1 | Bật detect tên nghi vấn |
| `antiraid_default_avatar_check` | 1 | Bật check default avatar |

---

## 💣 5. MODULE ANTI-NUKE

**Cách hoạt động:** mỗi khi có channel/role bị xóa/tạo, server bị đổi tên..., bot **tra Audit Log** (entry < 10 giây tuổi) tìm thủ phạm → đếm số hành động của người đó trong cửa sổ thời gian → vượt ngưỡng thì **trừng phạt**.

**Trừng phạt = gỡ toàn bộ role nguy hiểm** (admin, manage guild/channels/roles/webhooks, ban, kick) của kẻ phá + log + **auto restore** từ backup mới nhất (nếu bật).

| Hành vi | Ngưỡng mặc định | Phản ứng |
|---|---|---|
| Xóa channel | 3 / 10s | Revoke quyền + restore |
| Xóa role | 3 / 10s | Revoke quyền + restore |
| Tạo channel/role | 5 / 10s | Revoke quyền |
| Đổi tên/avatar server | 3 / 60s | Revoke quyền |
| Cấp role admin trái phép | ngay lập tức | **Thu hồi role admin của người nhận** + revoke quyền người cấp |
| Add bot không whitelist | ngay lập tức | **Kick bot** + revoke quyền người add (nếu Owner add → tự whitelist bot) |
| Tạo webhook | 3 / 60s | Xóa webhook + revoke quyền |

**Backup tự động:**
- Mỗi 6 giờ (`antinuke_backup_interval`), lưu JSON: roles (tên, quyền, màu, vị trí), categories, channels (topic, slowmode, NSFW, **toàn bộ permission overwrites**)
- Giữ tối đa **10 file backup/guild**, backup ngay lần đầu khi bot khởi động
- **Restore chỉ bù phần thiếu** (so theo tên) — không xóa/ghi đè thứ đang tồn tại

| Config key | Mặc định |
|---|---|
| `antinuke_channel_delete_limit/window` | 3 / 10 |
| `antinuke_role_delete_limit/window` | 3 / 10 |
| `antinuke_create_limit/window` | 5 / 10 |
| `antinuke_guild_update_limit/window` | 3 / 60 |
| `antinuke_webhook_limit/window` | 3 / 60 |
| `antinuke_admin_grant_protect` | 1 |
| `antinuke_bot_add_protect` | 1 |
| `antinuke_backup_interval` | 21600 (6h) |
| `antinuke_auto_restore` | 1 |

---

## 🔁 6. MODULE ANTI-SPAM

**Cách hoạt động:** mọi tin nhắn đi qua chuỗi check; dính check nào → **xóa tin + warn** (cooldown 1 warn/10s/user để spam 20 tin không nhảy thẳng lên ban).

**Chống loãng chat:** mỗi user chỉ bị nhắc trong kênh **đúng 1 lần** (tin nhắc tự xóa sau 5s); tái phạm các lần sau bot **chỉ xóa tin + ghi vào channel log**, không gửi gì vào kênh chat nữa. (Anti-content cũng hoạt động y hệt.)

| # | Check | Ngưỡng mặc định | Config key |
|---|---|---|---|
| 1 | Tin lặp lại | 5 tin/5s | `antispam_dup_limit/window` |
| 2 | Similarity (SequenceMatcher) | >80% giống = lặp | `antispam_similarity` |
| 3 | Mass mention | @everyone/@here hoặc >5 mention | `antispam_mention_limit` |
| 4 | Emoji spam | >10 emoji/tin | `antispam_emoji_limit` |
| 5 | Caps spam | >70% hoa, tin >10 ký tự | `antispam_caps_percent/minlen` |
| 6 | Auto slowmode | >10 tin/10s/channel → slowmode 10s | `antispam_slowmode_msgs/window/duration` |
| 7 | Mass spam | 20+ người cùng nội dung/30s → 🔒 **lockdown channel** | `antispam_mass_users/window` |
| 8 | Từ lặp trong câu | 1 từ >5 lần | `antispam_word_repeat` |

Check 6–7 là cấp channel (không phạt cá nhân). Channel bị lockdown mở lại bằng `/unlock`.

---

## 🈲 7. MODULE ANTI-CONTENT

**Cách hoạt động:** check theo thứ tự nguy hiểm giảm dần; dính → xóa + warn + log chi tiết (kèm domain/code vi phạm).

| # | Check | Cơ chế | Config key |
|---|---|---|---|
| 1 | 🎣 Phishing | So domain với list từ GitHub (Discord-AntiScam/scam-links), **tự refresh mỗi 6h**, lỗi mạng giữ list cũ | `anticontent_phishing` |
| 2 | 💎 Nitro scam | Domain chứa `discord`/`nitro`/`dlscord`/`disc0rd`... nhưng không phải domain Discord thật → typosquat | `anticontent_nitro_scam` |
| 3 | 🔗 Invite lạ | Bắt `discord.gg / discord.com/invite / dsc.gg`; **cho phép** invite của chính server (cache 5 phút) + invite whitelist | `anticontent_invite_block` |
| 4 | 🔞 NSFW | Keyword trong domain + text (word boundary); bỏ qua channel đã đánh dấu NSFW | `anticontent_nsfw` |
| 5 | 📛 Blacklist từ | `blacklist.txt` (global, mọi server) **+** bảng SQLite (per-guild qua `/blacklist add`) | luôn chạy |
| 6 | 🈲 CJK | Block ký tự Trung (Hán tự) / Nhật (Kana) / Hàn (Hangul) | `anticontent_block_cjk` |

**Quản lý từ cấm:**
```
/blacklist add <từ>     ← cấm từ cho server này (lưu SQLite)
/blacklist remove <từ>
/blacklist list
/blacklist reload       ← reload file blacklist.txt sau khi sửa tay
```

---

## ⚠️ 8. WARN SYSTEM

**Cách hoạt động:** warn tích lũy theo **user + guild**, lưu SQLite. Mỗi warn mới → đếm tổng warn còn hiệu lực → áp thang phạt:

| Warn thứ | Hình phạt |
|---|---|
| 1 | 📩 DM cảnh báo |
| 2 | 🔇 Mute 10 phút (`warn_mute1_duration`) |
| 3 | 🔇 Mute 1 giờ (`warn_mute2_duration`) |
| 4 | 👢 Kick |
| 5+ | 🔨 Ban vĩnh viễn |

- **Warn tự hết hạn sau 30 ngày** (`warn_expire_days`) — task chạy mỗi giờ
- **Mute sống sót qua restart**: thời điểm unmute lưu DB, task check mỗi 30s
- Mọi module anti-* đều phạt qua warn system → vi phạm liên tục tự leo thang
- Owner/Co-owner/whitelist **không thể bị warn** (kể cả mod cố tình `/warn`)

| Lệnh | Quyền | Tác dụng |
|---|---|---|
| `/warn <user> [lý do]` | Moderate Members | Warn thủ công |
| `/warns <user>` | Moderate Members | Xem warn còn hiệu lực |
| `/clearwarns <user>` | Administrator | Xóa toàn bộ warn |
| `/unmute <user>` | Moderate Members | Gỡ mute sớm |

---

## 📋 9. LOG SYSTEM

Đặt channel log: `/setlog #channel`. Toàn bộ log là embed có màu phân loại + footer avatar/ID người liên quan + timestamp.

| Sự kiện | Chi tiết kèm theo |
|---|---|
| 📥📤 Join / Leave | Tuổi account; roles khi rời |
| 🤖 Bot add/remove | **Ai add bot** (tra audit log) |
| 🗑️ Message delete | **Nội dung gốc** + link file đính kèm |
| ✏️ Message edit | Trước / Sau + link nhảy tới tin |
| 🔨 Ban / Unban | Người thực hiện |
| ⚠️🔇👢 Warn / Mute / Kick | Lý do + mod + lần warn thứ mấy |
| 🎭 Role member thay đổi | Ai cấp/thu hồi role gì |
| ⚠️ **Cấp quyền ADMIN** | Log riêng màu tím — cả khi member nhận role admin lẫn khi role được thêm quyền admin |
| ➕➖ Channel/Role tạo/xóa/đổi tên | Người thực hiện |
| 🪝 Webhook tạo/sửa/xóa | Channel + người liên quan |
| 🏠 Server đổi tên/avatar | Trước/sau + người làm |
| 🚨 Mọi sự kiện anti-* | Lockdown, quarantine, nuke detect, phishing... |

> 🔍 **Hành động của Owner/Co-owner vẫn log đầy đủ** — bypass hình phạt chứ không bypass audit.

---

## ⚙️ 10. LỆNH CẤU HÌNH

### `/config` (quyền Administrator)
```
/config view [module]        ← xem toàn bộ config (đánh dấu * nếu đã chỉnh khác mặc định)
/config set <key> <value>    ← chỉnh threshold bất kỳ, hiệu lực NGAY không cần restart
/config toggle <module>      ← bật/tắt từng module: antiraid/antinuke/antispam/anticontent/warn/log
```

Ví dụ thực tế:
```
/config set antispam_emoji_limit 15        ← nới emoji lên 15
/config set antiraid_min_account_age 259200 ← yêu cầu account 3 ngày tuổi
/config set warn_expire_days 7             ← warn hết hạn sau 7 ngày
/config toggle anticontent                 ← tắt hẳn lọc nội dung
```

### Lockdown & khóa channel
| Lệnh | Quyền | Tác dụng |
|---|---|---|
| `/lockdown` | Whitelist/Owner | 🚨 Khóa send_messages **toàn bộ channel** |
| `/unlockdown` | Whitelist/Owner | Mở khóa toàn server (trả overwrite về mặc định) |
| `/lock` | Manage Channels | Khóa channel hiện tại |
| `/unlock` | Manage Channels | Mở channel hiện tại (dùng sau khi anti-spam tự lockdown) |

### Backup & restore
| Lệnh | Quyền | Tác dụng |
|---|---|---|
| `/backup` | Owner/Co-owner | Backup thủ công ngay |
| `/restore` | Owner/Co-owner | Restore từ backup mới nhất (chỉ bù phần thiếu) |

### Rate limit lệnh
Mọi slash command bị giới hạn **10 lệnh/phút/người** (`ratelimit_mod_commands/window`) — chống mod bị hack spam lệnh. Owner/Co-owner/whitelist bypass.

---

## 🧠 11. KIẾN TRÚC — CÁCH CÁC MODULE NÓI CHUYỆN VỚI NHAU

```
                    ┌─────────────┐
   tin nhắn ──────► │  anti_spam  │──┐
                    │ anti_content│  │  vi phạm
                    └─────────────┘  ▼
                    ┌──────────────────────┐     ┌────────┐
   member join ───► │ anti_raid            │────►│ Warn   │── thang phạt
                    └──────────────────────┘     │ System │   DM→mute→kick→ban
                    ┌──────────────────────┐     └───┬────┘
   audit log  ────► │ anti_nuke            │         │
                    └──────────┬───────────┘         ▼
                               │ revoke + restore ┌────────┐
                               └─────────────────►│ Logger │──► #channel-log
                                  mọi sự kiện     └────────┘
```

- **`WarnSystem.warn_user()`** — API trung tâm: mọi module phạt qua đây, thang phạt tự leo
- **`Logger.send_log()`** — API log chung: embed màu + field + footer
- **`AntiRaid.lockdown_guild()/unlockdown_guild()`** — dùng chung cho mass-join tự động và lệnh `/lockdown`
- **Database có cache RAM** (config + whitelist) — event xử lý nhanh, không query SQLite mỗi tin nhắn
- **Mỗi listener bọc try/except riêng** — một module lỗi không kéo sập bot; load cog lỗi cũng chỉ mất module đó

---

## 🚑 12. XỬ LÝ SỰ CỐ THƯỜNG GẶP

| Vấn đề | Nguyên nhân / Cách sửa |
|---|---|
| Bot không kick/mute được | Role bot thấp hơn role đối tượng → kéo role bot lên cao |
| Anti-nuke không tìm ra thủ phạm | Bot thiếu quyền **View Audit Log** |
| Không có log | Chưa `/setlog`, hoặc `log_channel_id` trỏ channel đã xóa |
| Slash command không hiện | Đợi sync (~1 phút), hoặc kick bot ra invite lại đúng scope `applications.commands` |
| Captcha không gửi được | User đóng DM + server không có system channel → user bị quarantine (đúng thiết kế) |
| Thành viên cũ bị check captcha | Chỉ check khi **join** — member đang ở trong server không bị ảnh hưởng |
| Muốn tắt 1 tính năng lẻ | Đa số có toggle riêng: `/config set anticontent_block_cjk 0`, `/config set antiraid_default_avatar_check 0`... |
| Restore không trả lại tin nhắn | Backup chỉ lưu **cấu trúc** (role/channel/permission) — tin nhắn không backup được qua API |
| Bot bị rate limit Discord | Lockdown/restore server lớn chạy chậm là bình thường (mỗi channel 1 API call) |

---

## 📝 13. TÓM TẮT TOÀN BỘ LỆNH

| Lệnh | Quyền tối thiểu |
|---|---|
| `/config view / set / toggle` | Administrator |
| `/setlog <channel>` | Administrator |
| `/setup-verify <channel> [role]` | Administrator |
| `/warn`, `/warns`, `/unmute` | Moderate Members |
| `/clearwarns` | Administrator |
| `/blacklist add / remove / list / reload` | Administrator |
| `/lock`, `/unlock` | Manage Channels |
| `/lockdown`, `/unlockdown` | Whitelist / Owner / Co-owner |
| `/whitelist add / remove / list` | **Owner / Co-owner** |
| `/whitelistbot add / remove` | **Owner / Co-owner** |
| `/backup`, `/restore` | **Owner / Co-owner** |

---

*Mọi threshold lưu SQLite per-guild — chỉnh bằng `/config set` là áp dụng ngay, không cần restart bot.*
