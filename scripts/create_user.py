#!/usr/bin/env python3
"""创建用户账号"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server.database import create_user, init_db
from server.auth import hash_password

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    args = p.parse_args()
    init_db()
    create_user(args.username, hash_password(args.password))
    print(f"User {args.username} created")
