import asyncio
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

api_id = 26046830
api_hash = "2e9c6ba203f15c52fd7a749ea6de09db"

client = TelegramClient("test", api_id, api_hash)

async def main():
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print("Уже авторизован:", me.username or me.id)
        await client.disconnect()
        return

    try:
        qr = await client.qr_login()
        print("\nСканируй QR: телефон → Настройки → Устройства → Подключить устройство\n")
        try:
            import qrcode
            q = qrcode.QRCode()
            q.add_data(qr.url)
            q.print_ascii(invert=True)
        except ImportError:
            print("Ссылка для QR:", qr.url)
        await qr.wait()
    except SessionPasswordNeededError:
        pw = input("\nВведи облачный пароль (2FA): ")
        await client.sign_in(password=pw)

    me = await client.get_me()
    print("\n✅ Готово! Авторизован:", me.username or me.id)
    await client.disconnect()

asyncio.run(main())