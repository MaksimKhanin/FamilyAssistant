"""Create a family and its first member.

    python -m scripts.seed --family "Хaнины" --name Марина --username marina --password ...

Run once after the first `docker compose up`. Everything else — the rest of the
family, module flags, cameras — is done from the panel itself.
"""
import argparse
import sys

from app.core.auth import hash_password
from app.core.db import create_all, session_scope
from app.core.family import get_settings
from app.core.models import ROLE_HEAD, Family, User
from app.modules import load_modules


def main():
    parser = argparse.ArgumentParser(description="Создать семью и главу семьи")
    parser.add_argument("--family", default="Семья", help="Название семьи")
    parser.add_argument("--name", required=True, help="Имя главы семьи, например «Марина»")
    parser.add_argument("--username", required=True, help="Логин для входа в панель")
    parser.add_argument("--password", required=True, help="Пароль для входа в панель")
    parser.add_argument("--relation", default="", help="Кем приходится, например «мама»")
    args = parser.parse_args()

    load_modules()      # чтобы create_all увидел таблицы модулей
    create_all()

    with session_scope() as db:
        if db.query(User).filter(User.username == args.username).one_or_none() is not None:
            print(f"Пользователь {args.username} уже есть — ничего не меняю")
            return 1

        family = Family(name=args.family)
        db.add(family)
        db.flush()

        head = User(
            family_id=family.id,
            username=args.username,
            password_hash=hash_password(args.password),
            display_name=args.name,
            relation=args.relation or None,
            role=ROLE_HEAD,
            avatar_slot=0,
            autonomy=1,
        )
        db.add(head)
        db.flush()
        get_settings(db, family.id)

        print(f"Готово. Семья «{family.name}», глава семьи {head.display_name} (@{head.username}).")
        print("Заходите в панель и добавляйте остальных — каждому достанется своя ссылка-приглашение.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
