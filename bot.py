import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN', '').strip()
GUILD_ID = int(os.getenv('GUILD_ID', '0') or 0)
SENIOR_ROLE_ID = int(os.getenv('SENIOR_ROLE_ID', '1570353093495881740') or 0)
REVIEW_CHANNEL_ID = int(os.getenv('REVIEW_CHANNEL_ID', '0') or 0)
AUDIT_CHANNEL_ID = int(os.getenv('AUDIT_CHANNEL_ID', '0') or 0)
DB_PATH = os.getenv('DB_PATH', 'warehouse.sqlite3')

if not TOKEN:
    raise RuntimeError('Не указан DISCORD_TOKEN в файле .env')

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS inventory (
            item TEXT PRIMARY KEY COLLATE NOCASE,
            quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS counters (
            kind TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO counters(kind,value) VALUES ('request',0),('delivery',0);
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            number INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            department TEXT NOT NULL DEFAULT '',
            items TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            photos TEXT NOT NULL DEFAULT '',
            extra TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Ожидает рассмотрения',
            reviewer_id INTEGER,
            reviewer_name TEXT,
            decision_reason TEXT NOT NULL DEFAULT '',
            review_message_id INTEGER,
            review_channel_id INTEGER,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            photo_thread_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            details TEXT NOT NULL,
            actor_id INTEGER NOT NULL,
            actor_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        ''')
        # Миграция старой базы: отдельная ветка для фотографий поставки.
        cols = {row['name'] for row in c.execute('PRAGMA table_info(records)').fetchall()}
        if 'photo_thread_id' not in cols:
            c.execute('ALTER TABLE records ADD COLUMN photo_thread_id INTEGER')


def now():
    return datetime.now(timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M:%S %Z')


def is_senior(member) -> bool:
    return isinstance(member, discord.Member) and any(r.id == SENIOR_ROLE_ID for r in member.roles)


def parse_items(raw: str):
    """Accept one item per line: name - quantity, or name: quantity."""
    result = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^(.+?)\s*(?:-|:|—)\s*(\d+)\s*$', line)
        if not m:
            raise ValueError(f'Неверная строка: «{line}». Формат: Предмет - количество')
        name, qty = m.group(1).strip(), int(m.group(2))
        if not name or qty <= 0:
            raise ValueError('Название предмета и количество должны быть корректными.')
        if len(name) > 80:
            raise ValueError('Название предмета не должно превышать 80 символов.')
        result.append((name, qty))
    if not result:
        raise ValueError('Укажи хотя бы один предмет.')
    merged = {}
    for name, qty in result:
        key = name.casefold()
        if key in merged:
            merged[key] = (merged[key][0], merged[key][1] + qty)
        else:
            merged[key] = (name, qty)
    return list(merged.values())


def fmt_items(items):
    return '\n'.join(f'• **{discord.utils.escape_markdown(n)}** — `{q}` шт.' for n, q in items)


def next_number(kind):
    with db() as c:
        c.execute('UPDATE counters SET value=value+1 WHERE kind=?', (kind,))
        row = c.execute('SELECT value FROM counters WHERE kind=?', (kind,)).fetchone()
        return row['value']


def add_audit(action, details, actor):
    with db() as c:
        c.execute('INSERT INTO audit(action,details,actor_id,actor_name,created_at) VALUES(?,?,?,?,?)',
                  (action, details, actor.id, str(actor), now()))


async def send_audit(guild, title, description, color=discord.Color.dark_red()):
    if not AUDIT_CHANNEL_ID:
        return
    channel = guild.get_channel(AUDIT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(AUDIT_CHANNEL_ID)
        except discord.HTTPException:
            return
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    await channel.send(embed=embed)


async def send_review(guild, embed, view):
    if not REVIEW_CHANNEL_ID:
        raise RuntimeError('Не задан REVIEW_CHANNEL_ID в .env')
    channel = guild.get_channel(REVIEW_CHANNEL_ID)
    if channel is None:
        channel = await guild.fetch_channel(REVIEW_CHANNEL_ID)
    msg = await channel.send(content=f'<@&{SENIOR_ROLE_ID}>', embed=embed, view=view, allowed_mentions=discord.AllowedMentions(roles=True))
    return msg


class WarehouseRequestModal(discord.ui.Modal, title='Запрос со склада'):
    full_name = discord.ui.TextInput(label='Имя и фамилия', placeholder='Например: Иван Иванов', max_length=80)
    department = discord.ui.TextInput(label='Подразделение', placeholder='Например: ОО / ОВБ', max_length=60)
    items = discord.ui.TextInput(label='Предметы и количество', style=discord.TextStyle.paragraph,
                                 placeholder='Пистолет - 2\nДефибриллятор - 1', max_length=1000)
    reason = discord.ui.TextInput(label='Причина запроса', style=discord.TextStyle.paragraph, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            parsed = parse_items(str(self.items))
            number = next_number('request')
            embed = discord.Embed(title=f'📦 ЗАПРОС №{number:03d}', color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
            embed.add_field(name='Заявитель', value=str(self.full_name), inline=True)
            embed.add_field(name='Подразделение', value=str(self.department), inline=True)
            embed.add_field(name='Запрошенное имущество', value=fmt_items(parsed), inline=False)
            embed.add_field(name='Причина', value=str(self.reason), inline=False)
            embed.set_footer(text=f'ID пользователя: {interaction.user.id} • Ожидает решения старшего состава')
            record_id = None
            with db() as c:
                cur = c.execute('''INSERT INTO records(kind,number,user_id,user_name,department,items,reason,created_at)
                    VALUES('request',?,?,?,?,?,?,?)''',
                    (number, interaction.user.id, str(self.full_name), str(self.department), '\n'.join(f'{n}|{q}' for n,q in parsed), str(self.reason), now()))
                record_id = cur.lastrowid
            view = DecisionView(record_id, 'request')
            msg = await send_review(interaction.guild, embed, view)
            with db() as c:
                c.execute('UPDATE records SET review_message_id=?, review_channel_id=? WHERE id=?', (msg.id, msg.channel.id, record_id))
            await interaction.response.send_message(f'✅ Запрос №{number:03d} отправлен старшему составу на рассмотрение.', ephemeral=True)
        except (ValueError, RuntimeError, discord.HTTPException) as e:
            if interaction.response.is_done():
                await interaction.followup.send(f'❌ {e}', ephemeral=True)
            else:
                await interaction.response.send_message(f'❌ {e}', ephemeral=True)


class DeliveryModal(discord.ui.Modal, title='Отчёт о поставке'):
    full_name = discord.ui.TextInput(label='Имя и фамилия', max_length=80)
    department = discord.ui.TextInput(label='Подразделение', max_length=60)
    items = discord.ui.TextInput(label='Поставленные предметы и количество', style=discord.TextStyle.paragraph,
                                 placeholder='Пистолет - 5\nАптечка - 10', max_length=1000)
    extra = discord.ui.TextInput(label='Дополнительная информация', style=discord.TextStyle.paragraph, required=False, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            parsed = parse_items(str(self.items))
            number = next_number('delivery')
            embed = discord.Embed(title=f'🚚 ПОСТАВКА №{number:03d}', color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
            embed.add_field(name='Ответственный', value=str(self.full_name), inline=True)
            embed.add_field(name='Подразделение', value=str(self.department), inline=True)
            embed.add_field(name='Поставлено', value=fmt_items(parsed), inline=False)
            embed.add_field(name='Дополнительная информация', value=str(self.extra) or 'Не указана', inline=False)
            embed.add_field(name='Фотоотчёт', value='⏳ Ожидаются фотографии от автора в отдельной ветке. До проверки фото поставка не будет одобрена.', inline=False)
            embed.set_footer(text=f'ID пользователя: {interaction.user.id} • Ожидает проверки старшего состава')
            with db() as c:
                cur = c.execute("""INSERT INTO records(kind,number,user_id,user_name,department,items,extra,created_at)
                    VALUES('delivery',?,?,?,?,?,?,?)""",
                    (number, interaction.user.id, str(self.full_name), str(self.department), '\n'.join(f'{n}|{q}' for n,q in parsed), str(self.extra), now()))
                record_id = cur.lastrowid
            msg = await send_review(interaction.guild, embed, DecisionView(record_id, 'delivery'))
            # Ветка привязана к заявке: автор прикладывает туда реальные файлы изображений Discord.
            thread = await msg.create_thread(name=f'Фото поставки №{number:03d}')
            await thread.send(
                f'📸 {interaction.user.mention}, прикрепите сюда фотографии поставки **№{number:03d}** как изображения/файлы Discord. '
                'Старший состав проверит фото в этой ветке и затем примет решение по заявке.'
            )
            embed.add_field(name='Ветка с фотографиями', value=thread.mention, inline=False)
            await msg.edit(embed=embed)
            with db() as c:
                c.execute('UPDATE records SET review_message_id=?, review_channel_id=?, photo_thread_id=? WHERE id=?',
                          (msg.id, msg.channel.id, thread.id, record_id))
            add_audit('Создан отчёт о поставке', f'ПОСТАВКА №{number:03d}: {fmt_items(parsed)}', interaction.user)
            await interaction.followup.send(
                f'✅ Отчёт о поставке №{number:03d} отправлен старшему составу. Теперь прикрепи фотографии в ветке {thread.mention}.',
                ephemeral=True
            )
            await send_audit(interaction.guild, 'Новый отчёт о поставке',
                             f'**ПОСТАВКА №{number:03d}**\nАвтор: {interaction.user.mention}\n{fmt_items(parsed)}\nФото: {thread.mention}')
        except (ValueError, RuntimeError, discord.HTTPException) as e:
            if interaction.response.is_done():
                await interaction.followup.send(f'❌ {e}', ephemeral=True)
            else:
                await interaction.response.send_message(f'❌ {e}', ephemeral=True)


class RestockModal(discord.ui.Modal, title='Пополнение общего склада'):
    items = discord.ui.TextInput(label='Предметы и количество', style=discord.TextStyle.paragraph,
                                 placeholder='Пистолет - 10\nАптечка - 20', max_length=1000)
    note = discord.ui.TextInput(label='Основание / примечание', style=discord.TextStyle.paragraph, required=False, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_senior(interaction.user):
            return await interaction.response.send_message('⛔ Пополнять склад может только старший состав.', ephemeral=True)
        try:
            parsed = parse_items(str(self.items))
            changes = []
            with db() as c:
                for name, qty in parsed:
                    c.execute('''INSERT INTO inventory(item,quantity,updated_at) VALUES(?,?,?)
                        ON CONFLICT(item) DO UPDATE SET quantity=quantity+excluded.quantity, updated_at=excluded.updated_at''',
                        (name, qty, now()))
                    row = c.execute('SELECT quantity FROM inventory WHERE item=? COLLATE NOCASE', (name,)).fetchone()
                    changes.append(f'• **{name}**: +{qty} шт. → остаток `{row["quantity"]}`')
            details = '\n'.join(changes)
            add_audit('Пополнение склада', details + (f'\nОснование: {self.note}' if str(self.note) else ''), interaction.user)
            await interaction.response.send_message('✅ Склад пополнен. Остатки обновлены.', ephemeral=True)
            await send_audit(interaction.guild, '📥 Пополнение общего склада', f'Старший состав: {interaction.user.mention}\n{details}\nОснование: {str(self.note) or "Не указано"}', discord.Color.green())
        except ValueError as e:
            await interaction.response.send_message(f'❌ {e}', ephemeral=True)


class ApproveModal(discord.ui.Modal, title='Причина одобрения'):
    reason = discord.ui.TextInput(
        label='Укажи причину одобрения',
        style=discord.TextStyle.paragraph,
        placeholder='Например: имущество выдано по назначению / поставка подтверждена',
        required=True,
        max_length=500
    )
    def __init__(self, record_id: int, kind: str):
        super().__init__()
        self.record_id, self.kind = record_id, kind
    async def on_submit(self, interaction: discord.Interaction):
        await decide(interaction, self.record_id, self.kind, True, str(self.reason))


class RejectModal(discord.ui.Modal, title='Причина отклонения'):
    reason = discord.ui.TextInput(label='Укажи причину', style=discord.TextStyle.paragraph, max_length=500)
    def __init__(self, record_id: int, kind: str):
        super().__init__()
        self.record_id, self.kind = record_id, kind
    async def on_submit(self, interaction: discord.Interaction):
        await decide(interaction, self.record_id, self.kind, False, str(self.reason))


async def decide(interaction: discord.Interaction, record_id: int, kind: str, approved: bool, reason: str = ''):
    if not is_senior(interaction.user):
        return await interaction.response.send_message('⛔ Решение доступно только старшему составу.', ephemeral=True)
    with db() as c:
        rec = c.execute('SELECT * FROM records WHERE id=? AND kind=?', (record_id, kind)).fetchone()
        if rec is None:
            return await interaction.response.send_message('Запись не найдена.', ephemeral=True)
        if rec['status'] != 'Ожидает рассмотрения':
            return await interaction.response.send_message('Это обращение уже рассмотрено.', ephemeral=True)
        items = []
        for line in rec['items'].splitlines():
            n, q = line.rsplit('|', 1)
            items.append((n, int(q)))
        if approved and kind == 'delivery':
            thread_id = rec['photo_thread_id']
            if not thread_id:
                return await interaction.response.send_message('❌ Для этой поставки не создана ветка фотоотчёта. Попросите автора оформить поставку заново.', ephemeral=True)
            try:
                photo_thread = interaction.guild.get_thread(thread_id) or await interaction.guild.fetch_channel(thread_id)
                has_author_photo = False
                async for post in photo_thread.history(limit=100, oldest_first=True):
                    if post.author.id != rec['user_id']:
                        continue
                    if any((attachment.content_type or '').startswith('image/') or
                           attachment.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif'))
                           for attachment in post.attachments):
                        has_author_photo = True
                        break
                if not has_author_photo:
                    return await interaction.response.send_message('❌ Нельзя одобрить поставку без фото. Проверьте ветку фотоотчёта: автор должен прикрепить туда хотя бы одно изображение.', ephemeral=True)
            except discord.HTTPException:
                return await interaction.response.send_message('❌ Не удалось проверить фотографии. Убедитесь, что бот видит ветку фотоотчёта.', ephemeral=True)
        if approved and kind == 'request':
            shortages = []
            for name, qty in items:
                row = c.execute('SELECT quantity FROM inventory WHERE item=? COLLATE NOCASE', (name,)).fetchone()
                available = row['quantity'] if row else 0
                if available < qty:
                    shortages.append(f'{name}: нужно {qty}, в наличии {available}')
            if shortages:
                return await interaction.response.send_message('❌ Недостаточно имущества на складе:\n' + '\n'.join(shortages) + '\nПополните склад или отклоните запрос.', ephemeral=True)
            for name, qty in items:
                c.execute('UPDATE inventory SET quantity=quantity-?, updated_at=? WHERE item=? COLLATE NOCASE', (qty, now(), name))
        if approved and kind == 'delivery':
            for name, qty in items:
                c.execute('''INSERT INTO inventory(item,quantity,updated_at) VALUES(?,?,?)
                    ON CONFLICT(item) DO UPDATE SET quantity=quantity+excluded.quantity, updated_at=excluded.updated_at''', (name, qty, now()))
        status = 'Одобрено' if approved else 'Отклонено'
        c.execute('UPDATE records SET status=?, reviewer_id=?, reviewer_name=?, decision_reason=?, decided_at=? WHERE id=?',
                  (status, interaction.user.id, str(interaction.user), reason, now(), record_id))
        number = rec['number']
        kind_label = 'ЗАПРОС' if kind == 'request' else 'ПОСТАВКА'
        updated_items = []
        for name, qty in items:
            row = c.execute('SELECT quantity FROM inventory WHERE item=? COLLATE NOCASE', (name,)).fetchone()
            updated_items.append(f'{name}: {row["quantity"] if row else 0} шт. на складе')
    action = f'{kind_label} №{number:03d} — {status}. Проверил: {interaction.user}. Причина: {reason or "—"}'
    if kind != 'request':
        add_audit('Решение по обращению', action, interaction.user)
    for child in interaction.message.components:
        pass
    try:
        old = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(title=f'{kind_label} №{number:03d}')
        embed = discord.Embed.from_dict(old.to_dict())
        embed.color = discord.Color.green() if approved else discord.Color.red()
        embed.add_field(name='Решение', value=f'**{status}**\nРассмотрел: {interaction.user.mention}\nПричина: {reason or "Не указана"}', inline=False)
        if approved and kind == 'request':
            embed.add_field(name='Остатки после выдачи', value='\n'.join(updated_items), inline=False)
        await interaction.message.edit(embed=embed, view=None)
    except discord.HTTPException:
        pass
    await interaction.response.send_message(f'✅ {kind_label} №{number:03d}: {status.lower()}.', ephemeral=True)
    if kind != 'request':
        await send_audit(interaction.guild, f'{"✅" if approved else "❌"} {kind_label} №{number:03d}: {status}',
                         f'Проверил: {interaction.user.mention}\nПричина: {reason or "Не указана"}\n' + ('\n'.join(updated_items) if updated_items else ''),
                         discord.Color.green() if approved else discord.Color.red())
    try:
        user = interaction.guild.get_member(rec['user_id']) or await bot.fetch_user(rec['user_id'])
        await user.send(f'{"✅" if approved else "❌"} Ваш {kind_label.lower()} №{number:03d} {status.lower()}.\nПричина: {reason or "Не указана"}')
    except (discord.HTTPException, AttributeError):
        pass


class DecisionView(discord.ui.View):
    def __init__(self, record_id: int, kind: str):
        super().__init__(timeout=None)
        self.record_id, self.kind = record_id, kind
        prefix = f'wh:{kind}:{record_id}'
        self.approve.custom_id = prefix + ':yes'
        self.reject.custom_id = prefix + ':no'
    @discord.ui.button(label='Одобрить', style=discord.ButtonStyle.success, emoji='✅')
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApproveModal(self.record_id, self.kind))
    @discord.ui.button(label='Отклонить', style=discord.ButtonStyle.danger, emoji='❌')
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RejectModal(self.record_id, self.kind))


class RenameInventoryModal(discord.ui.Modal, title='Переименовать позицию'):
    old_name = discord.ui.TextInput(label='Текущее название', max_length=80)
    new_name = discord.ui.TextInput(label='Новое название', max_length=80)
    async def on_submit(self, interaction: discord.Interaction):
        if not is_senior(interaction.user):
            return await interaction.response.send_message('⛔ Доступно только старшему составу.', ephemeral=True)
        old, new = str(self.old_name).strip(), str(self.new_name).strip()
        if not old or not new:
            return await interaction.response.send_message('❌ Оба названия обязательны.', ephemeral=True)
        try:
            with db() as c:
                row = c.execute('SELECT quantity FROM inventory WHERE item=? COLLATE NOCASE', (old,)).fetchone()
                if row is None:
                    return await interaction.response.send_message('❌ Такая позиция не найдена.', ephemeral=True)
                duplicate = c.execute('SELECT 1 FROM inventory WHERE item=? COLLATE NOCASE', (new,)).fetchone()
                if duplicate:
                    return await interaction.response.send_message('❌ Позиция с таким названием уже существует.', ephemeral=True)
                c.execute('UPDATE inventory SET item=?, updated_at=? WHERE item=? COLLATE NOCASE', (new, now(), old))
            details = f'Позиция «{old}» переименована в «{new}». Остаток сохранён: {row["quantity"]} шт.'
            add_audit('Переименование позиции', details, interaction.user)
            await interaction.response.send_message('✅ Название позиции изменено.', ephemeral=True)
            await send_audit(interaction.guild, '✏️ Переименование позиции склада', f'Старший состав: {interaction.user.mention}\n{details}')
        except sqlite3.IntegrityError:
            await interaction.response.send_message('❌ Не удалось переименовать позицию: такое название уже занято.', ephemeral=True)


class DeleteInventoryModal(discord.ui.Modal, title='Удалить позицию'):
    item_name = discord.ui.TextInput(label='Название позиции для удаления', max_length=80)
    async def on_submit(self, interaction: discord.Interaction):
        if not is_senior(interaction.user):
            return await interaction.response.send_message('⛔ Доступно только старшему составу.', ephemeral=True)
        name = str(self.item_name).strip()
        with db() as c:
            row = c.execute('SELECT quantity FROM inventory WHERE item=? COLLATE NOCASE', (name,)).fetchone()
            if row is None:
                return await interaction.response.send_message('❌ Такая позиция не найдена.', ephemeral=True)
            c.execute('DELETE FROM inventory WHERE item=? COLLATE NOCASE', (name,))
        details = f'Позиция «{name}» удалена. Остаток перед удалением: {row["quantity"]} шт.'
        add_audit('Удаление позиции', details, interaction.user)
        await interaction.response.send_message('✅ Позиция удалена со склада.', ephemeral=True)
        await send_audit(interaction.guild, '🗑️ Удаление позиции склада', f'Старший состав: {interaction.user.mention}\n{details}')


class SetInventoryQuantityModal(discord.ui.Modal, title='Изменить остаток'):
    item_name = discord.ui.TextInput(label='Название позиции', max_length=80)
    quantity = discord.ui.TextInput(label='Новое количество (0 или больше)', max_length=10)
    note = discord.ui.TextInput(label='Причина изменения', style=discord.TextStyle.paragraph, max_length=300)
    async def on_submit(self, interaction: discord.Interaction):
        if not is_senior(interaction.user):
            return await interaction.response.send_message('⛔ Доступно только старшему составу.', ephemeral=True)
        name = str(self.item_name).strip()
        try:
            qty = int(str(self.quantity).strip())
            if qty < 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message('❌ Количество должно быть целым числом от 0 и выше.', ephemeral=True)
        with db() as c:
            row = c.execute('SELECT quantity FROM inventory WHERE item=? COLLATE NOCASE', (name,)).fetchone()
            if row is None:
                return await interaction.response.send_message('❌ Такая позиция не найдена. Сначала добавьте её через пополнение.', ephemeral=True)
            old_qty = row['quantity']
            c.execute('UPDATE inventory SET quantity=?, updated_at=? WHERE item=? COLLATE NOCASE', (qty, now(), name))
        details = f'«{name}»: {old_qty} → {qty} шт. Причина: {self.note}'
        add_audit('Изменение остатка', details, interaction.user)
        await interaction.response.send_message('✅ Остаток изменён.', ephemeral=True)
        await send_audit(interaction.guild, '⚖️ Изменение остатка склада', f'Старший состав: {interaction.user.mention}\n{details}')


class InventoryManagementSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label='Добавить позицию / количество', value='add', emoji='➕'),
            discord.SelectOption(label='Переименовать позицию', value='rename', emoji='✏️'),
            discord.SelectOption(label='Изменить остаток', value='quantity', emoji='⚖️'),
            discord.SelectOption(label='Удалить позицию', value='delete', emoji='🗑️'),
        ]
        super().__init__(placeholder='Выберите управление складом...', min_values=1, max_values=1,
                         options=options, custom_id='warehouse:inventory_manage')
    async def callback(self, interaction: discord.Interaction):
        if not is_senior(interaction.user):
            return await interaction.response.send_message('⛔ Управление складом доступно только старшему составу.', ephemeral=True)
        action = self.values[0]
        if action == 'add':
            return await interaction.response.send_modal(RestockModal())
        if action == 'rename':
            return await interaction.response.send_modal(RenameInventoryModal())
        if action == 'quantity':
            return await interaction.response.send_modal(SetInventoryQuantityModal())
        if action == 'delete':
            return await interaction.response.send_modal(DeleteInventoryModal())


class InventoryManagementView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(InventoryManagementSelect())


class StockView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
    @discord.ui.button(label='Обновить остатки', style=discord.ButtonStyle.secondary, emoji='🔄')
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=stock_embed(), view=self)


def stock_embed():
    with db() as c:
        rows = c.execute('SELECT item,quantity FROM inventory ORDER BY item COLLATE NOCASE').fetchall()
    embed = discord.Embed(title='📦 ОБЩИЙ СКЛАД ПГО «КАРАТЕЛИ»', color=discord.Color.dark_teal(), timestamp=datetime.now(timezone.utc))
    if not rows:
        embed.description = 'Склад пока пуст. Старший состав может пополнить его через кнопку «Пополнить склад».'
    else:
        embed.description = '\n'.join(f'• **{discord.utils.escape_markdown(r["item"])}** — `{r["quantity"]}` шт.' for r in rows)[:4000]
    embed.set_footer(text=f'Позиций: {len(rows)} • Остатки обновляются после одобрения операций')
    return embed


def history_embed(limit=15):
    with db() as c:
        rows = c.execute("SELECT * FROM records WHERE kind='delivery' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        audit = c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    e = discord.Embed(title='📚 ИСТОРИЯ СКЛАДА', color=discord.Color.dark_purple(), timestamp=datetime.now(timezone.utc))
    if not rows and not audit:
        e.description = 'История пока пуста.'
        return e
    lines = []
    for r in rows:
        label = 'ЗАПРОС' if r['kind'] == 'request' else 'ПОСТАВКА'
        lines.append(f'**{label} №{r["number"]:03d}** — {r["status"]} • {r["user_name"]} • {r["created_at"]}')
        if r['reviewer_name']:
            lines.append(f'↳ Рассмотрел: {r["reviewer_name"]}; {r["decision_reason"] or "без примечания"}')
    e.description = '\n'.join(lines)[:4000]
    return e


class WarehouseActionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label='Запросить имущество', value='request', emoji='📦', description='Оформить заявку на получение со склада'),
            discord.SelectOption(label='Оформить поставку', value='delivery', emoji='🚚', description='Зарегистрировать поставку и приложить фотографии'),
            discord.SelectOption(label='Посмотреть остатки', value='stock', emoji='📋', description='Проверить наличие имущества на складе'),
            discord.SelectOption(label='Пополнить склад', value='restock', emoji='➕', description='Добавить количество — только старший состав'),
            discord.SelectOption(label='Управление позициями', value='manage', emoji='⚙️', description='Добавлять, переименовывать и удалять позиции'),
            discord.SelectOption(label='История операций', value='history', emoji='📚', description='Посмотреть журнал склада — только старший состав'),
        ]
        super().__init__(placeholder='Выберите действие со складом...', min_values=1, max_values=1,
                         options=options, custom_id='warehouse:action_select')

    async def callback(self, interaction: discord.Interaction):
        action = self.values[0]
        if action == 'request':
            return await interaction.response.send_modal(WarehouseRequestModal())
        if action == 'delivery':
            return await interaction.response.send_modal(DeliveryModal())
        if action == 'stock':
            return await interaction.response.send_message(embed=stock_embed(), view=StockView(), ephemeral=True)
        if action == 'restock':
            if not is_senior(interaction.user):
                return await interaction.response.send_message('⛔ Пополнять склад может только старший состав.', ephemeral=True)
            return await interaction.response.send_modal(RestockModal())
        if action == 'manage':
            if not is_senior(interaction.user):
                return await interaction.response.send_message('⛔ Управление складом доступно только старшему составу.', ephemeral=True)
            return await interaction.response.send_message('⚙️ **Управление позициями склада**\nВыберите действие:', view=InventoryManagementView(), ephemeral=True)
        if action == 'history':
            if not is_senior(interaction.user):
                return await interaction.response.send_message('⛔ Полная история доступна только старшему составу.', ephemeral=True)
            return await interaction.response.send_message(embed=history_embed(), ephemeral=True)


class WarehouseMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(WarehouseActionSelect())


class WarehousePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Открыть склад', style=discord.ButtonStyle.primary,
                       emoji='📦', custom_id='warehouse:open')
    async def open_warehouse(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title='📦 СКЛАД И СНАБЖЕНИЕ',
            description='Выберите нужное действие в выпадающем меню ниже.',
            color=discord.Color.from_rgb(125, 20, 30)
        )
        await interaction.response.send_message(embed=embed, view=WarehouseMenuView(), ephemeral=True)


@bot.event
async def on_ready():
    init_db()
    bot.add_view(WarehousePanel())
    # Persistent decision buttons are restored for every pending record.
    with db() as c:
        pending = c.execute("SELECT id,kind FROM records WHERE status='Ожидает рассмотрения'").fetchall()
    for r in pending:
        bot.add_view(DecisionView(r['id'], r['kind']))
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    print(f'Бот склада запущен: {bot.user} | ожидающих обращений: {len(pending)}')


@bot.tree.command(name='warehouse', description='Опубликовать панель склада ПГО «Каратели»')
@app_commands.guild_only()
async def warehouse(interaction: discord.Interaction):
    if not is_senior(interaction.user):
        return await interaction.response.send_message('⛔ Опубликовать панель может только старший состав.', ephemeral=True)
    embed = discord.Embed(title='📦 ПГО «КАРАТЕЛИ» | СКЛАД И СНАБЖЕНИЕ',
                          description='Для работы со складом нажмите кнопку **«Открыть склад»**.\n'
                                      'В открывшемся меню выберите нужное действие.',
                          color=discord.Color.from_rgb(125, 20, 30))
    embed.set_footer(text='Единый склад • Запросы и поставки проходят проверку старшего состава')
    await interaction.response.send_message(embed=embed, view=WarehousePanel())


init_db()
bot.run(TOKEN)
