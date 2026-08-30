import argparse
import json
import os
import time
from random import choice

import pyotp
import requests
from dotenv import load_dotenv

from takion_castle import TakionCastle

load_dotenv()

RECAPTCHA_SITE_KEY = "6LdXDboZAAAAADkqigoAnCWJdvxT8ogLhcEAv_uK"


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def signin_headers(castle_token: str, referer: str, captcha_token: str = None) -> dict:
    """The XHR header set signin.rockstargames.com sends. Order is part of the fingerprint,
    Castle reads it, so do not sort it or move x-castle-request-token."""
    headers = {
        "sec-ch-ua-platform": '"macOS"',
        "rockstar-clientid": "rsg",
        "x-castle-request-token": castle_token,
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        "sec-ch-ua-mobile": "?0",
        "x-lang": "en-US",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "content-type": "application/json",
        "accept": "*/*",
        "origin": "https://signin.rockstargames.com",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": referer,
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    }
    if captcha_token:
        headers["x-captchatoken-recaptchaenterprise"] = captcha_token
    return headers


if __name__ == "__main__":
    if not os.path.exists("proxies.txt"):
        raise Exception("Create and fill proxies.txt with one proxy per line in format:\nip:port\nor\nip:port:user:pass")
    # Credentials come from the CLI when passed, otherwise from the environment. The secret
    # is only needed when the account has 2FA turned on.
    parser = argparse.ArgumentParser(description="Log into a Rockstar account, plain or 2FA.")
    parser.add_argument("--mail", "--email", dest="mail")
    parser.add_argument("--password")
    parser.add_argument("--secret", help="TOTP secret, only for a 2FA account")
    args = parser.parse_args()

    api_key = require_env("TAKION_API_KEY")
    email = args.mail or require_env("ROCKSTAR_EMAIL")
    password = args.password or require_env("ROCKSTAR_PASSWORD")
    totp_secret = args.secret or os.getenv("ROCKSTAR_2FA_SECRET")
    proxy = choice([line for line in open("proxies.txt").read().splitlines() if line.strip()])
    print(f"proxy: {proxy.split(':')[0]}...")

    solver = TakionCastle(
        api_key,
        proxy,
        client_hello="chrome150",
    )

    # [1] Resolve the exit-IP geo and pin the token to it, so the session is coherent
    print("[1] Resolving the geo of the exit IP...")
    country, timezone = solver.detect_geo()
    print(f"    country={country} timezone={timezone or '(country default)'}\n")

    # [2] Solve reCAPTCHA v3 Enterprise for the SignIn action, through the Takion mirror.
    # It bills to the same API key, each solve costs 3 requests.
    print("[2] Solving reCAPTCHA v3 Enterprise (SignIn)...")
    create_response = requests.post(
        "https://castle.takionapi.tech/extra/recaptcha/create",
        params={"api_key": api_key},
        json={
            "type": "recaptcha_v3_enterprise",
            "websiteURL": "https://signin.rockstargames.com/signin/user-form?cid=rsg",
            "websiteKey": RECAPTCHA_SITE_KEY,
            "pageAction": "SignIn",
        },
        timeout=30,
    )
    if create_response.status_code != 200:
        raise RuntimeError(f"/recaptcha/create failed [{create_response.status_code}]: {create_response.text[:200]}")
    created = create_response.json()
    if error := created.get("error"):
        raise RuntimeError(f"/recaptcha/create failed: {error}")
    task_id = created["taskId"]

    captcha_token = None
    for _ in range(45):
        time.sleep(2)
        pull_response = requests.post(
            "https://castle.takionapi.tech/extra/recaptcha/pull",
            params={"api_key": api_key},
            json={"taskId": task_id},
            timeout=30,
        )
        if pull_response.status_code != 200:
            raise RuntimeError(f"/recaptcha/pull failed [{pull_response.status_code}]: {pull_response.text[:200]}")
        pulled = pull_response.json()
        if error := pulled.get("error"):
            raise RuntimeError(f"/recaptcha/pull failed: {error}")
        if pulled.get("status") == "ready":
            captcha_token = (pulled.get("solution") or {}).get("gRecaptchaResponse")
            break
    if not captcha_token:
        raise TimeoutError("the reCAPTCHA solve did not finish within 90 seconds")
    print(f"    token={captcha_token[:60]}...\n")

    # [3] Submit the password login. A plain account signs in on this call, a 2FA account
    # answers 200 with useMfa=true and an mfaToken instead.
    print(f"[3] Signing in as {email}...")
    login_response = solver.post(
        "https://signin.rockstargames.com/api/login/rsg",
        headers=signin_headers(
            solver.generate_token("rockstar", country=country, timezone=timezone),
            "https://signin.rockstargames.com/signin/user-form?cid=rsg&returnUrl=%2F",
            captcha_token=captcha_token,
        ),
        data=json.dumps({
            "email": email,
            "password": password,
            "keepMeSignedIn": True,
            "deviceName": "Chrome on Mac",
            "returnUrl": "/",
            "linkInfo": {
                "shouldLink": False,
                "service": None,
                "username": None,
                "serviceVisibility": None,
            },
            "events": [
                {
                    "eventName": "Sign In Form",
                    "eventType": "page-view",
                },
            ],
        }),
    )
    login_result = login_response.json()
    print(f"    status={login_response.status_code} body={json.dumps(login_result)[:200]}\n")
    if login_response.status_code != 200:
        error_code = str((login_result.get("data") or {}).get("errorCode", ""))
        if error_code.startswith("1.500"):
            # 1.500.x is the Castle refusal, not a credentials error. The IP is burnt, a
            # retry on the same proxy returns the same thing.
            print("[BLOCKED] Castle refused the session, rotate to a clean proxy and run again")
        else:
            print(f"[FAIL] login rejected [{login_response.status_code}]: {login_result.get('message', login_result)}")
        raise SystemExit(1)

    if not login_result.get("useMfa"):
        # Plain account, already signed in on the login call above.
        print(f"[PASS] signed in as {email}")
        print(f"    session cookies: {', '.join(cookie.name for cookie in solver.cookiejar)}")
        raise SystemExit(0)

    # The account has 2FA, so the login above only handed back an mfaToken. Finish the
    # authenticator flow, this needs the TOTP secret saved at enrolment.
    if not totp_secret:
        print("[FAIL] this account has 2FA, pass --secret or set ROCKSTAR_2FA_SECRET")
        raise SystemExit(1)
    totp = pyotp.TOTP(totp_secret)
    mfa_token = login_result["mfaToken"]
    print(f"    2FA required (mfaType={login_result.get('mfaType')}), mfaToken={mfa_token[:40]}...\n")

    # [4] List the MFA devices on the account and pick the authenticator one
    print("[4] Listing MFA devices...")
    devices_response = solver.post(
        "https://signin.rockstargames.com/api/login/mfaDevices/rsg",
        headers=signin_headers(
            solver.generate_token("rockstar", country=country, timezone=timezone),
            "https://signin.rockstargames.com/signin/mfa-form?cid=rsg&returnUrl=%2F",
        ),
        data=json.dumps({"mfaToken": mfa_token}),
    )
    devices = devices_response.json().get("mfaDevices") or []
    print(f"    devices={devices}\n")
    authenticator = next((device for device in devices if device.get("deviceType") == "GoogleAuthenticator"), None)
    if not authenticator:
        print(f"[FAIL] no GoogleAuthenticator device on this account, devices={devices}")
        raise SystemExit(1)
    device_id = authenticator["deviceId"]

    # [5] Ask Rockstar to prime the device. For an authenticator this sends nothing, it just
    # hands back a refreshed mfaToken that the final step has to use.
    print("[5] Priming the authenticator device...")
    send_code_response = solver.post(
        "https://signin.rockstargames.com/api/login/mfaSendCode/rsg",
        headers=signin_headers(
            solver.generate_token("rockstar", country=country, timezone=timezone),
            "https://signin.rockstargames.com/signin/mfa-form?cid=rsg&returnUrl=%2F",
        ),
        data=json.dumps({
            "mfaToken": mfa_token,
            "mfaDeviceId": device_id,
        }),
    )
    mfa_token = send_code_response.json()["mfaToken"]
    print(f"    refreshed mfaToken={mfa_token[:40]}...\n")

    # [6] Compute the current TOTP code and complete the login
    mfa_code = totp.now()
    print(f"[6] Submitting the authenticator code {mfa_code}...")
    mfa_login_response = solver.post(
        "https://signin.rockstargames.com/api/mfaLogin/rsg",
        headers=signin_headers(
            solver.generate_token("rockstar", country=country, timezone=timezone),
            "https://signin.rockstargames.com/signin/mfa-form?cid=rsg&returnUrl=%2F",
        ),
        data=json.dumps({
            "email": email,
            "keepMeSignedIn": True,
            "linkInfo": {
                "shouldLink": False,
                "service": None,
                "username": None,
                "serviceVisibility": None,
            },
            "rememberMe": True,
            "deviceName": "Chrome on Mac",
            "mfaToken": mfa_token,
            "mfaCode": mfa_code,
            "mfaDeviceId": device_id,
            "returnUrl": "/",
            "events": [
                {
                    "eventName": "Sign In Form",
                    "eventType": "page-view",
                },
                {
                    "eventName": "MFA Form",
                    "eventType": "page-view",
                },
            ],
        }),
    )
    mfa_login_result = mfa_login_response.json()
    print(f"    status={mfa_login_response.status_code} body={json.dumps(mfa_login_result)[:200]}\n")

    if mfa_login_response.status_code == 200 and mfa_login_result.get("rockstarId"):
        print(f"[PASS] signed in with 2FA as {email}")
        print(f"    rockstarId={mfa_login_result.get('rockstarId')} nickname={mfa_login_result.get('nickname')} mfaEnabled={mfa_login_result.get('isMfaEnabled')}")
    else:
        print(f"[FAIL] 2FA login rejected [{mfa_login_response.status_code}]: {mfa_login_result.get('message', mfa_login_result)}")
        raise SystemExit(1)
