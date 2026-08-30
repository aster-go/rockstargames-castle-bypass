import argparse
import base64
import json
import os
import re
import string
import time
from datetime import UTC, datetime
from random import choice, choices

import pyotp
import requests
from dotenv import load_dotenv

from takion_castle import TakionCastle, proxy_url

load_dotenv()

DATE_OF_BIRTH = "1994-08-03T00:00:00.000Z"
RECAPTCHA_SITE_KEY = "6LdXDboZAAAAADkqigoAnCWJdvxT8ogLhcEAv_uK"
RECAPTCHA_RELEASE_PATTERN = re.compile(r"/recaptcha/releases/([\w\-]{20,26})/")
ACCOUNTS_FILE = "accounts_2fa.json"


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def signin_headers(castle_token: str, referer: str, captcha_token: str = None) -> dict:
    """The XHR header set the sign-up pages send. Order is part of the fingerprint, do not
    sort it or move x-castle-request-token."""
    headers = {
        "sec-ch-ua-platform": '"macOS"',
        "rockstar-clientid": "rsg",
        "x-castle-request-token": castle_token,
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "x-lang": "en-US",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
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


def parse_grecaptcha(text: str) -> list:
    """reCAPTCHA api3 answers are XSSI guarded with a leading )]}' line."""
    return json.loads(text.split("\n", 1)[1] if text.startswith(")]}'") else text)


def scapi_headers(bearer: str) -> dict:
    """The Social Club API is Bearer authenticated and lives on the www.rockstargames.com
    origin, not signin. No Castle token here, the JWT is the credential."""
    return {
        "sec-ch-ua-platform": '"macOS"',
        "authorization": f"Bearer {bearer}",
        "x-lang": "en-US",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "content-type": "application/json",
        "accept": "*/*",
        "origin": "https://www.rockstargames.com",
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": "https://www.rockstargames.com/",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    }


if __name__ == "__main__":
    if not os.path.exists("proxies.txt"):
        raise Exception("Create and fill proxies.txt with one proxy per line in format:\nip:port\nor\nip:port:user:pass")
    # Account details come from the CLI when passed, otherwise from the environment. The
    # nickname defaults to a random one when neither is given.
    parser = argparse.ArgumentParser(description="Register a Rockstar account.")
    parser.add_argument("--mail", "--email", dest="mail")
    parser.add_argument("--password")
    parser.add_argument("--nickname")
    args = parser.parse_args()

    api_key = require_env("TAKION_API_KEY")
    email = args.mail or require_env("ROCKSTAR_EMAIL")
    password = args.password or require_env("ROCKSTAR_PASSWORD")
    nickname = args.nickname or os.getenv("ROCKSTAR_NICKNAME") or "tk" + "".join(choices(string.ascii_lowercase + string.digits, k=10))
    proxy = choice([line for line in open("proxies.txt").read().splitlines() if line.strip()])
    proxies = {
        "http": proxy_url(proxy),
        "https": proxy_url(proxy),
    }
    print(f"proxy: {proxy.split(':')[0]}...")

    solver = TakionCastle(
        api_key,
        proxy,
        client_hello="chrome150",
    )

    # [1] Read the country and timezone behind the proxy and drive the whole sign-up off
    # them. Sign-up cross checks the token timezone, the country field and the exit IP, and
    # big countries (US, CA, AU) span several zones, so anything hardcoded mismatches most
    # IPs. Detecting it is how you never send a wrong country by hand.
    print("[1] Resolving the geo of the exit IP...")
    country, timezone = solver.detect_geo()
    print(f"    country={country} timezone={timezone or '(country default)'}\n")
    print(f"account: {email} (nickname {nickname}, country {country})")

    # [2] Load the sign-up page so the session carries the edge cookies a browser would
    print("[2] Warming up the session on the sign-up page...")
    page_response = solver.get(
        "https://signin.rockstargames.com/create/?cid=rsg&returnUrl=%2F",
        headers={
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-site": "none",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "upgrade-insecure-requests": "1",
            "accept-encoding": "gzip, deflate, br, zstd",
        },
    )
    print(f"    {page_response.status_code}, cookies: {', '.join(cookie.name for cookie in solver.cookiejar)}\n")

    # [3] Age gate. It runs before the form and already carries a Castle token.
    print("[3] Passing the age check...")
    age_response = solver.post(
        "https://signin.rockstargames.com/api/registration/age/check/rsg",
        headers=signin_headers(
            solver.generate_token("rockstar", country=country, timezone=timezone),
            "https://signin.rockstargames.com/create/date-of-birth?cid=rsg&returnUrl=%2F",
        ),
        data=json.dumps({"dateOfBirth": DATE_OF_BIRTH}),
    )
    age_result = age_response.json()
    print(f"    {age_response.status_code} isAMinor={age_result.get('isAMinor')}\n")
    if age_response.status_code != 200 or age_result.get("isAMinor") is not False:
        print(f"[FAIL] age check rejected [{age_response.status_code}]: {age_result}")
        raise SystemExit(1)

    # [4] Solve reCAPTCHA v3 Enterprise for the SignUp action, through the Takion mirror.
    # It bills to the same API key, each solve costs 3 requests.
    print("[4] Solving reCAPTCHA v3 Enterprise (SignUp)...")
    create_response = requests.post(
        "https://castle.takionapi.tech/extra/recaptcha/create",
        params={"api_key": api_key},
        json={
            "type": "recaptcha_v3_enterprise",
            "websiteURL": "https://signin.rockstargames.com/create/user-form?cid=rsg",
            "websiteKey": RECAPTCHA_SITE_KEY,
            "pageAction": "SignUp",
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

    # [5] Create the account. The events list replays the pages a real sign-up walks
    # through, Rockstar checks that the account was not created from a single POST.
    print("[5] Submitting the registration...")
    register_body = json.dumps({
        "nickname": nickname,
        "email": email,
        "password": password,
        "dob": DATE_OF_BIRTH,
        "country": country,
        "mailingList": False,
        "acceptedPolicies": {
            "tos": None,
            "pp": None,
        },
        "externalPlatformInfo": None,
        "activeTitleName": "",
        "linkInfo": {
            "shouldLink": False,
            "service": None,
            "username": None,
            "serviceVisibility": None,
        },
        "events": [
            {
                "eventName": "DOB",
                "eventType": "page-view",
            },
            {
                "eventName": "Legal Policy",
                "eventType": "page-view",
            },
            {
                "eventName": "New User Form",
                "eventType": "page-view",
            },
        ],
        "returnUrl": "/",
    })
    register_response = solver.post(
        "https://signin.rockstargames.com/api/registration/rsg",
        headers=signin_headers(
            # action=registration: this POST carries the sign-up form's telemetry, so the
            # token is shaped for the full form (DOB/checkbox, email+password) not a login.
            solver.generate_token("rockstar", country=country, timezone=timezone, action="registration"),
            "https://signin.rockstargames.com/create/user-form?cid=rsg&returnUrl=%2F",
            captcha_token=captcha_token,
        ),
        data=register_body,
    )
    register_result = register_response.json()
    print(f"    status={register_response.status_code} body={json.dumps(register_result)[:200]}\n")

    registration_mfa_token = register_result.get("mfaToken")
    if not registration_mfa_token:
        error_code = str((register_result.get("data") or {}).get("errorCode", ""))
        if error_code.startswith("1.500"):
            # 1.500.x on sign-up is almost always geo. The proxy country, the country field and the
            # token timezone have to be the same story, and some countries (GB) are refused
            # outright whatever you send.
            print("[BLOCKED] Rockstar refused the sign-up, check the proxy country and rotate the IP")
        else:
            print(f"[FAIL] registration rejected [{register_response.status_code}]: {register_result.get('message', register_result)}")
        raise SystemExit(1)

    # [6] The account exists but is unconfirmed. The OTP is not posted back to Rockstar,
    # it goes through Google reCAPTCHA account MFA, so pick up the current release token
    # and open the challenge that triggers the email.
    print("[6] Opening the reCAPTCHA account MFA challenge...")
    enterprise_js = requests.get(
        f"https://www.recaptcha.net/recaptcha/enterprise.js?render={RECAPTCHA_SITE_KEY}",
        headers={
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "referer": "https://signin.rockstargames.com/",
        },
        timeout=20,
    ).text
    release = RECAPTCHA_RELEASE_PATTERN.search(enterprise_js)
    if not release:
        raise RuntimeError("could not read the reCAPTCHA release token from enterprise.js")
    recaptcha_version = release.group(1)

    recaptcha_session = requests.Session()
    recaptcha_session.proxies = proxies
    recaptcha_headers = {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "accept": "*/*",
        "origin": "https://recaptcha.net",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": f"https://recaptcha.net/recaptcha/enterprise/anchor?ar=1&k={RECAPTCHA_SITE_KEY}&co=aHR0cHM6Ly9zaWduaW4ucm9ja3N0YXJnYW1lcy5jb206NDQz&hl=en&v={recaptcha_version}&size=invisible",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    }
    challenge_response = recaptcha_session.post(
        f"https://recaptcha.net/recaptcha/api3/accountchallenge?k={RECAPTCHA_SITE_KEY}",
        data=f"v={recaptcha_version}&avrt={registration_mfa_token}",
        headers=recaptcha_headers,
        timeout=30,
    )
    challenge = parse_grecaptcha(challenge_response.text)
    challenge_avrt = challenge[1]
    masked_email = challenge[2][0] if len(challenge) > 2 and isinstance(challenge[2], list) else email
    print(f"    v={recaptcha_version}, code sent to {masked_email}\n")

    # [7] Verify the emailed code. reCAPTCHA wants it base64 encoded with = swapped for a dot.
    print("[7] Verifying the emailed code...")
    verified_mfa_token = None
    for _ in range(3):
        otp = input("    enter the code from the inbox (blank to abort): ").strip()
        if not otp:
            break
        pin = base64.b64encode(json.dumps({"pin": otp}).encode()).decode().replace("=", ".")
        verify_response = recaptcha_session.post(
            f"https://recaptcha.net/recaptcha/api3/accountverify?k={RECAPTCHA_SITE_KEY}",
            data=f"v={recaptcha_version}&avrt={challenge_avrt}&response={pin}",
            headers=recaptcha_headers,
            timeout=30,
        )
        verified = parse_grecaptcha(verify_response.text)
        if len(verified) > 3 and verified[3]:
            verified_mfa_token = verified[3]
            break
        print("    code refused, try again")
    if not verified_mfa_token:
        print("[FAIL] the code was never accepted, the challenge expires after a few minutes")
        raise SystemExit(1)
    print(f"    verified={verified_mfa_token[:60]}...\n")

    # [8] Hand the verified token back to Rockstar, this is the call that creates the account
    print("[8] Confirming the account...")
    confirm_body = json.dumps({
        "nickname": nickname,
        "email": email,
        "password": password,
        "dob": DATE_OF_BIRTH,
        "country": country,
        "mailingList": False,
        "acceptedPolicies": {
            "tos": None,
            "pp": None,
        },
        "mfaType": "RE_EMAIL",
        "mfaToken": verified_mfa_token,
        "mfaReason": 3,
        "returnUrl": "",
        "linkInfo": {
            "shouldLink": False,
            "service": None,
            "username": None,
            "serviceVisibility": None,
        },
        "events": [
            {
                "eventName": "New User Form",
                "eventType": "page-view",
            },
            {
                "eventName": "DOB",
                "eventType": "page-view",
            },
            {
                "eventName": "Legal Policy",
                "eventType": "page-view",
            },
            {
                "eventName": "New User Form",
                "eventType": "page-view",
            },
            {
                "eventName": "Email Verification",
                "eventType": "page-view",
            },
        ],
        "externalPlatformInfo": None,
        "activeTitleName": "",
    })
    confirm_response = solver.post(
        "https://signin.rockstargames.com/api/registration/emailMfa/rsg",
        headers=signin_headers(
            solver.generate_token("rockstar", country=country, timezone=timezone),
            "https://signin.rockstargames.com/create/mfa-form?cid=rsg",
        ),
        data=confirm_body,
    )
    confirm_result = confirm_response.json()
    print(f"    status={confirm_response.status_code} body={json.dumps(confirm_result)[:200]}\n")

    if confirm_response.status_code != 201:
        print(f"[FAIL] confirmation rejected [{confirm_response.status_code}]: {confirm_result.get('message', confirm_result)}")
        raise SystemExit(1)

    print(f"[PASS] account created: {email}")
    print(f"    rockstarId={confirm_result.get('rockstarId')} nickname={confirm_result.get('nickname')}\n")

    # The account exists. Now enrol a TOTP authenticator on it and save the secret, so every
    # generated account comes out of this script already carrying its own 2FA. From here it is
    # the Social Club API, which is Bearer authenticated, not Castle.

    # [9] Turn the signed-in session into a Social Club Bearer. authorize primes the connect
    # flow, check hands back a one-time code, the gateway exchanges it and drops the
    # BearerToken cookie. The gateway answers on a 302, so do not follow it or the cookie is
    # lost.
    print("[9] Exchanging the session for a Social Club Bearer...")
    solver.get(
        "https://signin.rockstargames.com/connect/authorize/rsg?returnUrl=%2F",
        headers={
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "upgrade-insecure-requests": "1",
            "accept-encoding": "gzip, deflate, br, zstd",
        },
    )
    check_response = solver.post(
        "https://signin.rockstargames.com/api/connect/check/rsg",
        headers=signin_headers(
            solver.generate_token("rockstar", country=country, timezone=timezone),
            "https://signin.rockstargames.com/connect/authorize/rsg?returnUrl=%2F",
        ),
        data=json.dumps({"returnUrl": "/"}),
    )
    redirect_url = check_response.json().get("redirectUrl")
    if not redirect_url:
        print(f"[FAIL] connect/check gave no redirectUrl: {check_response.json()}")
        raise SystemExit(1)
    solver.get(
        redirect_url,
        headers={
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "upgrade-insecure-requests": "1",
            "accept-encoding": "gzip, deflate, br, zstd",
        },
        allow_redirects=False,
    )
    bearer = solver.cookiejar.get("BearerToken")
    if not bearer:
        print("[BLOCKED] the gateway BearerToken cookie was not captured. It is set on a 302, so the TLS endpoint has to return that hop instead of following it (allow_redirects=False). The account is created, only the 2FA enrolment is pending.")
        raise SystemExit(1)
    print(f"    bearer={bearer[:40]}...\n")

    # [10] Ask the Social Club to start an authenticator enrolment. It answers with the TOTP
    # secretKey and a one-time registration cookie that ties the confirm back to this request.
    print("[10] Requesting an authenticator secret...")
    # This POST has no body. The origin still wants an explicit Content-Length: 0, without it
    # it answers 411 Length Required, so set it by hand.
    request_response = solver.post(
        "https://scapi.rockstargames.com/account/requestRegisterMfa",
        headers={
            **scapi_headers(bearer),
            "content-length": "0",
        },
        data="",
    )
    if request_response.status_code != 200:
        print(f"[FAIL] requestRegisterMfa [{request_response.status_code}]: {request_response.text[:200]}")
        raise SystemExit(1)
    request_result = request_response.json()
    registration = request_result.get("result") or {}
    secret = registration.get("secretKey")
    registration_cookie = registration.get("mfaDeviceRegistrationCookie")
    if not secret or not registration_cookie:
        print(f"[FAIL] requestRegisterMfa gave no secret: {json.dumps(request_result)[:200]}")
        raise SystemExit(1)
    print(f"    secret={secret}\n")

    # [11] Confirm the enrolment with a live TOTP code off that secret. The password is
    # re-checked here, so it has to be the exact account password.
    mfa_code = pyotp.TOTP(secret).now()
    print(f"[11] Confirming enrolment with code {mfa_code}...")
    verify_response = solver.post(
        "https://scapi.rockstargames.com/account/verifyMfaRegistration",
        headers=scapi_headers(bearer),
        data=json.dumps({
            "code": mfa_code,
            "machineName": "Chrome on Mac",
            "mfaDeviceRegistrationCookie": registration_cookie,
            "password": password,
        }),
    )
    if verify_response.status_code != 200:
        print(f"[FAIL] verifyMfaRegistration [{verify_response.status_code}]: {verify_response.text[:200]}")
        raise SystemExit(1)
    verify_result = verify_response.json()
    print(f"    status={verify_response.status_code} body={json.dumps(verify_result)[:160]}\n")
    if not verify_result.get("status"):
        # A wrong-code or wrong-password enrolment answers 200 with status:false and an error
        # code (0.3400.15 was the wrong password in testing), so surface it rather than saving.
        print(f"[FAIL] verifyMfaRegistration rejected: {json.dumps(verify_result)[:200]}")
        raise SystemExit(1)

    # [12] Persist the account and its secret. Append to accounts_2fa.json so every run adds
    # one line, and print it so it is never only on disk.
    account = {
        "email": email,
        "password": password,
        "nickname": nickname,
        "totp_secret": secret,
        "rockstar_id": confirm_result.get("rockstarId"),
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    accounts = []
    if os.path.exists(ACCOUNTS_FILE):
        accounts = json.load(open(ACCOUNTS_FILE))
    accounts.append(account)
    json.dump(accounts, open(ACCOUNTS_FILE, "w"), indent=2)
    print(f"[PASS] 2FA enrolled and account saved to {ACCOUNTS_FILE}")
    print(f"    {json.dumps(account)}")
