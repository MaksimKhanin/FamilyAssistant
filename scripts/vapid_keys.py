"""Сгенерировать пару ключей VAPID для web push.

    python -m scripts.vapid_keys

Делается один раз при развёртывании. Ключи кладутся в .env; менять их потом
нельзя — все подписки устройств станут недействительными, и семье придётся
заново разрешать уведомления.
"""
from app.core.webpush import generate_vapid_keys


def main():
    private, public = generate_vapid_keys()
    print("Добавьте в .env:\n")
    print(f"VAPID_PUBLIC_KEY={public}")
    print(f"VAPID_PRIVATE_KEY={private}")
    print("VAPID_SUBJECT=mailto:вашпочта@example.com")
    print("\nПриватный ключ никому не показывайте: им подписываются уведомления.")


if __name__ == "__main__":
    main()
