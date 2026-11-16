import asyncio
import csv
import io
import logging
import math
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
                           BotCommand, BotCommandScopeChat)

from .db import Store


def load_env():
    # Process environment wins over the optional local file. No shell execution.
    env_path = Path(__file__).resolve().parent.parent / '.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())


def buttons(items):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)] for label, data in items])


def fee_valid(fee):
    try:
        value = Decimal(fee)
        return value.is_finite() and value > 0
    except InvalidOperation:
        return False



def status_label(status):
    return {'unpaid': 'در انتظار پرداخت', 'pending': 'در انتظار تأیید ادمین',
            'paid': 'ثبت‌نام تأییدشده', 'rejected': 'رسید ردشده'}[status]


def money(event):
    unit = 'تومان' if event['currency'] == 'IRT' else event['currency']
    return f"{Decimal(event['fee']):,} {unit}".translate(str.maketrans('0123456789,', '۰۱۲۳۴۵۶۷۸۹٬'))


async def send_lines(message, lines):
    chunk = ''
    for line in lines:
        if len(chunk) + len(line) > 3500:
            await message.answer(chunk)
            chunk = ''
        chunk += line + '\n'
    if chunk:
        await message.answer(chunk)


async def deliver_receipt(bot, chat_id, row):
    caption = (f"ثبت‌نام #{row['id']} — {row['event']}\n"
               f"{row['name']} {row['surname']} ({row['nickname']})\nشماره تماس: {row['phone']}")
    markup = buttons([('تأیید ثبت‌نام', f"approve:{row['id']}"),
                      ('رد رسید', f"reject:{row['id']}")])
    if row['receipt_kind'] == 'photo':
        await bot.send_photo(chat_id, row['receipt'], caption=caption, reply_markup=markup)
    else:
        await bot.send_document(chat_id, row['receipt'], caption=caption, reply_markup=markup)


async def main():
    load_env()
    token = os.getenv('BOT_TOKEN', '')
    if not token or token.startswith('replace_'):
        raise SystemExit('Set BOT_TOKEN in .env first; see README.md.')
    admins = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()}
    db = Store(os.getenv('DATABASE_PATH', 'data/boardgame_house.sqlite3'))
    if not db.one('SELECT id FROM events LIMIT 1'):
        db.run('INSERT INTO events(name,fee,currency,instructions) VALUES (?,?,?,?)',
               ('ریدرز', os.getenv('RAIDERS_FEE') or '400000', os.getenv('RAIDERS_CURRENCY', 'IRT'),
                os.getenv('PAYMENT_INSTRUCTIONS', '')))
    bot = Bot(token)
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    async def send_payment(message, registration):
        event = db.one('SELECT * FROM events WHERE id=?', (registration['event_id'],))
        if not fee_valid(event['fee']) or not event['instructions']:
            await message.answer('اطلاعات پرداخت هنوز تنظیم نشده است. اطلاعات شما ذخیره شد؛ با @hfmasaeli تماس بگیرید.')
            return
        db.draft(message.from_user.id, 'receipt', {'rid': registration['id']})
        await message.answer(f"{event['name']} — مبلغ ورودی: {money(event)}\n"
                             f"{event['instructions']}\n\nتصویر یا فایل PDF رسید را همین‌جا ارسال کنید تا به ادمین برسد. "
                             'ثبت‌نام فقط پس از تأیید ادمین قطعی می‌شود. برای لغو فرم: /cancel',
                             reply_markup=ReplyKeyboardRemove())

    @router.message(Command('start', 'help'))
    async def start(m):
        await m.answer('به بوردگیم هاوس خوش آمدید!\n/register — ثبت‌نام در مسابقه\n'
                       '/leaderboard — جدول رکوردها\n/profile — سوابق، امتیازها و قهرمانی‌های شما\n'
                       '/cancel — لغو فرم فعلی\n/whoami — شناسه تلگرام شما\n'
                       'برای برگزاری مسابقه، نام و شماره تلفن شما دریافت می‌شود. '
                       'فقط ادمین‌ها به این اطلاعات دسترسی دارند؛ جدول رکوردها فقط نام مستعار را نشان می‌دهد.')

    @router.message(Command('whoami'))
    async def whoami(m):
        await m.answer(f'شناسه تلگرام شما: {m.from_user.id}')

    @router.message(Command('cancel'))
    async def cancel(m):
        db.clear(m.from_user.id)
        await m.answer('فرم فعلی لغو شد. ثبت‌نام‌های ذخیره‌شده از طریق /register در دسترس هستند.', reply_markup=ReplyKeyboardRemove())

    @router.message(Command('register', 'leaderboard'))
    async def choose(m):
        mode = 'register' if m.text.split('@')[0] == '/register' else 'board'
        events = db.rows('SELECT * FROM events WHERE open=1' if mode == 'register' else "SELECT MIN(id) id, CASE WHEN series='raiders' THEN 'ریدرز' ELSE series END name FROM events GROUP BY series")
        await m.answer('مسابقه را انتخاب کنید:' if events else 'فعلاً دوره‌ای برای نمایش وجود ندارد.', reply_markup=buttons([
            (e['name'], f"{mode}:{e['id']}") for e in events]) if events else None)

    @router.callback_query(F.data.startswith('register:'))
    async def register(c):
        eid = int(c.data.split(':')[1])
        event = db.one('SELECT * FROM events WHERE id=? AND open=1', (eid,))
        await c.answer()
        if not event:
            await c.message.answer('ثبت‌نام این دوره بسته شده است.')
            return
        existing = db.one('SELECT * FROM registrations WHERE user_id=? AND event_id=?', (c.from_user.id, eid))
        if existing and existing['status'] in ('paid', 'pending'):
            await c.message.answer('ثبت‌نام شما تأیید شده است.' if existing['status'] == 'paid' else 'رسید شما در انتظار بررسی ادمین است.')
            return
        if existing:
            # Callback messages are authored by the bot; pass the actual user ID explicitly.
            db.draft(c.from_user.id, 'receipt', {'rid': existing['id']})
            if fee_valid(event['fee']) and event['instructions']:
                await c.message.answer(f"مبلغ ورودی: {money(event)}\n{event['instructions']}\nتصویر یا فایل PDF رسید را همین‌جا بفرستید تا برای ادمین ارسال شود.")
            else:
                await c.message.answer('اطلاعات پرداخت هنوز تنظیم نشده است. با @hfmasaeli تماس بگیرید.')
            return
        db.draft(c.from_user.id, 'name', {'eid': eid})
        await c.message.answer('نام شما چیست؟ اطلاعات تماس شما فقط در اختیار ادمین‌های مسابقه قرار می‌گیرد. برای لغو: /cancel')

    @router.callback_query(F.data.startswith('board:'))
    async def board(c):
        eid = int(c.data.split(':')[1])
        await c.answer()
        rows = db.leaderboard(eid)
        lines = ['جدول رکوردهای تمام دوره‌ها — بالاترین امتیاز یک دور برای هر بازیکن (۵۰ نفر برتر)']
        rank, previous = 0, None
        for i, row in enumerate(rows, 1):
            if row['best'] != previous:
                rank = i
            previous = row['best']
            lines.append(f"{rank}. {row['nickname']} — {row['best']:g}")
        await c.message.answer('\n'.join(lines) if rows else 'هنوز امتیازی ثبت نشده است.')

    @router.message(Command('profile'))
    async def profile(m):
        rows = db.rows('''SELECT e.name,r.status,MAX(s.score) best,COUNT(s.round) rounds
          FROM registrations r JOIN events e ON e.id=r.event_id
          LEFT JOIN scores s ON s.registration_id=r.id WHERE r.user_id=? GROUP BY r.id''', (m.from_user.id,))
        played = sum(bool(r['rounds']) for r in rows)
        lines = [f'پروفایل بوردگیم هاوس\nتعداد دوره‌های شرکت‌کرده با امتیاز ثبت‌شده: {played}']
        for r in rows:
            best = f"{r['best']:g}" if r['best'] is not None else 'ثبت نشده'
            lines.append(f"{r['name']}: {status_label(r['status'])}؛ بهترین امتیاز: {best}")
        records = db.rows('''SELECT e.series,MAX(s.score) best FROM registrations r
          JOIN events e ON e.id=r.event_id JOIN scores s ON s.registration_id=r.id
          WHERE r.user_id=? AND r.status='paid' GROUP BY e.series''', (m.from_user.id,))
        for record in records:
            game = 'ریدرز' if record['series'] == 'raiders' else record['series']
            lines.append(f"رکورد تمام دوره‌های {game}: {record['best']:g}")
        titles = db.titles(m.from_user.id)
        lines.append(f'🏆 تعداد قهرمانی‌ها: {len(titles)}')
        lines.extend(f"🏆 قهرمان {t['name']}" for t in titles)
        await send_lines(m, lines)

    @router.message(Command('admin', 'pending', 'players', 'score', 'event', 'close', 'open', 'export', 'payment', 'champion', 'unchampion'))
    async def admin(m):
        if m.from_user.id not in admins:
            await m.answer('این بخش مخصوص ادمین است. برای مشاهده شناسه خود از /whoami استفاده کنید.')
            return
        parts = m.text.split(maxsplit=1)
        command = parts[0].split('@')[0]
        arg = parts[1] if len(parts) > 1 else ''
        try:
            if command == '/admin':
                await m.answer('دستورهای ادمین:\n/pending — بررسی رسیدها\n/players EVENT_ID — فهرست ثبت‌نام‌ها\n'
                    '/score REGISTRATION_ID ROUND SCORE — ثبت یا اصلاح امتیاز یک دور\n'
                    '/event NAME | FEE | CURRENCY | PAYMENT INSTRUCTIONS — ایجاد دوره جدید ریدرز\n'
                    '/payment EVENT_ID | FEE | CURRENCY | INSTRUCTIONS — تنظیم پرداخت پیش از دریافت رسید\n'
                    '/close EVENT_ID یا /open EVENT_ID — بستن یا باز کردن ثبت‌نام\n/export EVENT_ID — خروجی خصوصی ثبت‌نام‌ها\n'
                    '/champion REGISTRATION_ID — ثبت یا جایگزینی تنها قهرمان دوره\n/unchampion REGISTRATION_ID — حذف قهرمانی\n'
                    'دوره‌ها:\n' + '\n'.join(f"{e['id']}: {e['name']} ({'باز' if e['open'] else 'بسته'})" for e in db.rows('SELECT * FROM events')))
            elif command == '/pending':
                rows = db.rows("SELECT r.*,e.name event FROM registrations r JOIN events e ON e.id=r.event_id WHERE status='pending' LIMIT 20")
                if not rows:
                    await m.answer('رسیدی در انتظار بررسی نیست.')
                for r in rows:
                    await deliver_receipt(bot, m.chat.id, r)
            elif command in ('/champion', '/unchampion'):
                db.champion(int(arg), m.from_user.id, remove=command == '/unchampion')
                await m.answer('عنوان قهرمانی در پروفایل به‌روزرسانی شد.')
            elif command == '/score':
                rid, round_name, value = arg.split()
                value = float(value)
                if not math.isfinite(value) or len(round_name) > 40:
                    raise ValueError('امتیاز باید عدد متناهی و نام دور حداکثر ۴۰ نویسه باشد.')
                db.score(int(rid), round_name, value, m.from_user.id)
                await m.answer('امتیاز ذخیره شد. جدول رکوردها و پروفایل به‌روز شدند.')
            elif command == '/event':
                name, fee, currency, instructions = [x.strip() for x in arg.split('|', 3)]
                if not name or len(name) > 100 or not fee_valid(fee) or not currency or not instructions:
                    raise ValueError('نام حداکثر ۱۰۰ نویسه، مبلغ مثبت، واحد پول و اطلاعات پرداخت را وارد کنید.')
                cur = db.run('INSERT INTO events(name,fee,currency,instructions) VALUES (?,?,?,?)', (name, fee, currency, instructions))
                await m.answer(f'دوره ایجاد شد: #{cur.lastrowid}.')
            elif command == '/payment':
                eid, fee, currency, instructions = [x.strip() for x in arg.split('|', 3)]
                if not fee_valid(fee) or not currency or not instructions:
                    raise ValueError('مبلغ مثبت، واحد پول و اطلاعات پرداخت را وارد کنید.')
                if db.one("SELECT id FROM registrations WHERE event_id=? AND status IN ('pending','paid')", (int(eid),)):
                    raise ValueError('دریافت رسید شروع شده است؛ برای تغییر مبلغ یک دوره جدید ایجاد کنید.')
                cur = db.run('UPDATE events SET fee=?,currency=?,instructions=? WHERE id=?', (fee, currency, instructions, int(eid)))
                await m.answer('اطلاعات پرداخت به‌روز شد.' if cur.rowcount else 'دوره پیدا نشد.')
            elif command in ('/close', '/open'):
                cur = db.run('UPDATE events SET open=? WHERE id=?', (int(command == '/open'), int(arg)))
                await m.answer('دوره به‌روز شد.' if cur.rowcount else 'دوره پیدا نشد.')
            else:
                rows = db.rows('SELECT id,user_id,name,surname,phone,nickname,status FROM registrations WHERE event_id=?', (int(arg),))
                if command == '/export':
                    stream = io.StringIO()
                    writer = csv.writer(stream)
                    writer.writerow(['registration_id', 'telegram_id', 'name', 'surname', 'phone', 'nickname', 'status'])
                    for r in rows:
                        # Neutralize spreadsheet formulas in user-supplied cells.
                        writer.writerow(["'" + str(v) if str(v).lstrip().startswith(('=', '+', '-', '@')) else v for v in r])
                    await m.answer_document(BufferedInputFile(stream.getvalue().encode('utf-8-sig'), filename=f'registrations-{int(arg)}.csv'))
                else:
                    for start in range(0, len(rows), 30):
                        await m.answer('\n'.join(f"#{r['id']} {r['nickname']} — {status_label(r['status'])}" for r in rows[start:start+30]))
                    if not rows:
                        await m.answer('ثبت‌نامی وجود ندارد.')
        except (ValueError, OverflowError):
            await m.answer('دستور نامعتبر است؛ قالب دستور را در /admin بررسی کنید.')

    @router.callback_query(F.data.startswith('approve:') | F.data.startswith('reject:'))
    async def review(c):
        if c.from_user.id not in admins:
            await c.answer('دسترسی ادمین لازم است.', show_alert=True)
            return
        action, rid = c.data.split(':')
        if not db.decide(int(rid), c.from_user.id, action == 'approve'):
            await c.answer('قبلاً بررسی شده است.', show_alert=True)
            return
        await c.answer('رسید بررسی شد.')
        await c.message.edit_reply_markup(reply_markup=None)
        row = db.one('SELECT user_id FROM registrations WHERE id=?', (int(rid),))
        try:
            await bot.send_message(row['user_id'], 'پرداخت تأیید شد! ثبت‌نام شما در مسابقه قطعی است.' if action == 'approve' else 'رسید رد شد. برای پیگیری با @hfmasaeli تماس بگیرید؛ سپس با /register رسید جدید بفرستید.')
        except Exception:
            logging.warning('Could not deliver payment decision for registration %s', rid)

    @router.message()
    async def form(m):
        uid = m.from_user.id
        step, data = db.get_draft(uid)
        if m.text and m.text.startswith('/'):
            await m.answer('دستور شناخته نشد. از /help یا /cancel استفاده کنید.')
            return
        if not step:
            await m.answer('از /register، /leaderboard یا /profile استفاده کنید.')
            return
        if step == 'receipt':
            row = db.one('SELECT * FROM registrations WHERE id=? AND user_id=?', (data['rid'], uid))
            event = db.one('SELECT * FROM events WHERE id=?', (row['event_id'],)) if row else None
            if not event or not event['open'] or not fee_valid(event['fee']) or not event['instructions']:
                await m.answer('ارسال رسید برای این دوره فعال نیست. با @hfmasaeli تماس بگیرید.')
                return
            receipt = m.photo[-1].file_id if m.photo else None
            kind = 'photo'
            if m.document and m.document.mime_type == 'application/pdf' and (m.document.file_size or 0) <= 10_000_000:
                receipt, kind = m.document.file_id, 'document'
            if not receipt:
                await m.answer('تصویر رسید یا فایل PDF حداکثر ۱۰ مگابایت ارسال کنید.')
                return
            result = db.run("UPDATE registrations SET receipt=?,receipt_kind=?,status='pending' WHERE id=? AND user_id=? AND status IN ('unpaid','rejected')", (receipt, kind, data['rid'], uid))
            db.clear(uid)
            await m.answer('رسید برای بررسی ادمین ثبت شد. پس از تأیید، ثبت‌نام شما قطعی می‌شود.' if result.rowcount else 'این رسید قبلاً ثبت شده است.')
            if result.rowcount:
                for aid in admins:
                    try:
                        registration = db.one('SELECT r.*,e.name event FROM registrations r JOIN events e ON e.id=r.event_id WHERE r.id=?', (data['rid'],))
                        await deliver_receipt(bot, aid, registration)
                    except Exception:
                        logging.warning('Could not notify an admin of a new receipt.')
            return
        value = (m.text or '').strip()
        if step == 'phone' and m.contact:
            if m.contact.user_id != uid:
                await m.answer('مخاطب خودتان را به اشتراک بگذارید یا شماره خود را بنویسید.')
                return
            value = m.contact.phone_number
        if step == 'phone':
            value = re.sub(r'[\s()\-]', '', value)
            if not re.fullmatch(r'\+?\d{8,15}', value):
                await m.answer('شماره تلفن معتبر وارد کنید؛ در صورت امکان همراه کد کشور.')
                return
        elif not value or len(value) > 60 or any(ord(ch) < 32 for ch in value):
            await m.answer('متن یک‌خطی بین ۱ تا ۶۰ نویسه وارد کنید.')
            return
        data[step] = value
        next_steps = {'name': ('surname', 'نام خانوادگی شما چیست؟'), 'surname': ('phone', 'شماره تلفن شما چیست؟'), 'phone': ('nickname', 'چه نام مستعاری در جدول رکوردها نمایش داده شود؟')}
        if step in next_steps:
            next_step, prompt = next_steps[step]
            db.draft(uid, next_step, data)
            keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='ارسال شماره تلفن من', request_contact=True)]], resize_keyboard=True, one_time_keyboard=True) if next_step == 'phone' else ReplyKeyboardRemove()
            await m.answer(prompt, reply_markup=keyboard)
        else:
            event = db.one('SELECT * FROM events WHERE id=? AND open=1', (data['eid'],))
            if not event:
                db.clear(uid)
                await m.answer('این دوره اکنون بسته شده است.', reply_markup=ReplyKeyboardRemove())
                return
            db.run('INSERT OR IGNORE INTO registrations(user_id,event_id,name,surname,phone,nickname) VALUES (?,?,?,?,?,?)',
                   (uid, data['eid'], data['name'], data['surname'], data['phone'], data['nickname']))
            row = db.one('SELECT * FROM registrations WHERE user_id=? AND event_id=?', (uid, data['eid']))
            db.clear(uid)
            await send_payment(m, row)

    dp = Dispatcher()
    dp.include_router(router)
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in [
            ('register', 'ثبت‌نام در مسابقه'), ('leaderboard', 'مشاهده جدول رکوردها'),
            ('profile', 'سوابق و قهرمانی‌های شما'), ('cancel', 'لغو فرم فعلی'), ('help', 'راهنما')]])
        admin_commands = [BotCommand(command=c, description=d) for c, d in [
            ('admin', 'راهنمای مدیریت و فهرست دوره‌ها'), ('pending', 'رسیدهای در انتظار بررسی'),
            ('players', 'فهرست بازیکنان یک دوره'), ('score', 'ثبت یا اصلاح امتیاز'),
            ('event', 'ایجاد دوره جدید'), ('payment', 'تنظیم اطلاعات پرداخت'),
            ('close', 'بستن ثبت‌نام دوره'), ('open', 'باز کردن ثبت‌نام دوره'),
            ('export', 'خروجی اطلاعات ثبت‌نام'), ('champion', 'تعیین قهرمان'),
            ('unchampion', 'حذف عنوان قهرمانی'), ('whoami', 'نمایش شناسه تلگرام'),
            ('register', 'ثبت‌نام در مسابقه'), ('leaderboard', 'مشاهده جدول رکوردها'),
            ('profile', 'سوابق و قهرمانی‌های شما'), ('cancel', 'لغو فرم فعلی'), ('help', 'راهنما')]]
        for admin_id in admins:
            try:
                await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception:
                logging.warning('Could not set an admin command menu; start the bot in a private chat and restart it.')
        await dp.start_polling(bot)
    finally:
        db.db.close()
        await bot.session.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
