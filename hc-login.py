#!/usr/bin/env python3
# This directly follows the OAuth login flow that is opaquely described
# https://github.com/openid/AppAuth-Android
# A really nice walk through of how it works is:
# https://auth0.com/docs/get-started/authentication-and-authorization-flow/call-your-api-using-the-authorization-code-flow-with-pkce
import io
import json
import re
import sys
from base64 import urlsafe_b64encode as base64url_encode
from urllib.parse import parse_qs, urlencode, urlparse
from zipfile import ZipFile

import requests
from bs4 import BeautifulSoup
from Crypto.Hash import SHA256
from Crypto.Random import get_random_bytes

from HADiscovery import augment_device_features
from HCxml2json import xml2json

# These two lines enable debugging at httplib level (requests->urllib3->http.client)
# You will see the REQUEST, including HEADERS and DATA, and RESPONSE with HEADERS but without DATA.
# The only thing missing will be the response.body which is not logged.
# http_client.HTTPConnection.debuglevel = 1

# You must initialize logging, otherwise you'll not see debug output.
# logging.basicConfig()
# logging.getLogger().setLevel(logging.DEBUG)
# requests_log = logging.getLogger("requests.packages.urllib3")
# requests_log.setLevel(logging.DEBUG)
# requests_log.propagate = True


def debug(*args):
    print(*args, file=sys.stderr)


email = sys.argv[1]
password = sys.argv[2]

headers = {"User-Agent": "hc-login/1.0"}

session = requests.Session()
session.headers.update(headers)

base_url = "https://api.home-connect.com/security/oauth/"
asset_urls = [
    "https://prod.reu.rest.homeconnectegw.com/",  # EU
    "https://prod.rna.rest.homeconnectegw.com/",  # US
]

#
# Start by fetching the old login page, which gives
# us the verifier and challenge for getting the token,
# even after the singlekey detour.
#
# The app_id and scope are hardcoded in the application
app_id = "9B75AC9EC512F36C84256AC47D813E2C1DD0D6520DF774B020E1E6E2EB29B1F3"
scope = [
    "ReadAccount",
    "Settings",
    "IdentifyAppliance",
    "Control",
    "DeleteAppliance",
    "WriteAppliance",
    "ReadOrigApi",
    "Monitor",
    "WriteOrigApi",
    "Images",
]
scope = [
    "ReadOrigApi",
]


def b64(b):
    return re.sub(r"=", "", base64url_encode(b).decode("UTF-8"))


def b64random(num):
    return b64(base64url_encode(get_random_bytes(num)))


verifier = b64(get_random_bytes(32))

login_query = {
    "response_type": "code",
    "prompt": "login",
    "code_challenge": b64(SHA256.new(verifier.encode("UTF-8")).digest()),
    "code_challenge_method": "S256",
    "client_id": app_id,
    "scope": " ".join(scope),
    "nonce": b64random(16),
    "state": b64random(16),
    "redirect_uri": "hcauth://auth/prod",
    "redirect_target": "icore",
}

loginpage_url = base_url + "authorize?" + urlencode(login_query)
token_url = base_url + "token"

debug(f"{loginpage_url=}")
r = session.get(loginpage_url)
if r.status_code != requests.codes.ok:
    print("error fetching login url!", loginpage_url, r.text, file=sys.stderr)
    exit(1)

# get the session from the text
if not (match := re.search(r'"sessionId" value="(.*?)"', r.text)):
    print("Unable to find session id in login page")
    exit(1)
session_id = match[1]
if not (match := re.search(r'"sessionData" value="(.*?)"', r.text)):
    print("Unable to find session data in login page")
    exit(1)
session_data = match[1]

debug("--------")

# now that we have a session id, contact the
# single key host to start the new login flow
singlekey_host = "https://singlekey-id.com"
login_url = singlekey_host + "/auth/en-us/log-in/"

preauth_url = singlekey_host + "/auth/connect/authorize"
preauth_query = {
    "client_id": "11F75C04-21C2-4DA9-A623-228B54E9A256",
    "redirect_uri": "https://api.home-connect.com/security/oauth/redirect_target",
    "response_type": "code",
    "scope": "openid email profile offline_access homeconnect.general",
    "prompt": "login",
    "style_id": "bsh_hc_01",
    "state": '{"session_id":"' + session_id + '"}',  # important: no spaces!
}

# fetch the preauth state to get the final callback url
preauth_url += "?" + urlencode(preauth_query)

# loop until we have the callback url
while True:
    debug(f"next {preauth_url=}")
    r = session.get(preauth_url, allow_redirects=False)
    if r.status_code == 200:
        break
    if r.status_code > 300 and r.status_code < 400:
        preauth_url = r.headers["location"]
        # Make relative locations absolute
        if not bool(urlparse(preauth_url).netloc):
            preauth_url = singlekey_host + preauth_url
        continue
    print(f"2: {preauth_url=}: failed to fetch {r} {r.text}", file=sys.stderr)
    exit(1)

# get the ReturnUrl from the response
query = parse_qs(urlparse(preauth_url).query)
return_url = query["ReturnUrl"][0]
debug(f"{return_url=}")

if "X-CSRF-FORM-TOKEN" in r.cookies:
    headers["RequestVerificationToken"] = r.cookies["X-CSRF-FORM-TOKEN"]
session.headers.update(headers)

debug("--------")

soup = BeautifulSoup(r.text, "html.parser")
requestVerificationToken = soup.find("input", {"name": "__RequestVerificationToken"}).get("value")
r = session.post(
    preauth_url,
    data={
        "UserIdentifierInput.EmailInput.StringValue": email,
        "__RequestVerificationToken": requestVerificationToken,
    },
    allow_redirects=False,
)

password_url = r.headers["location"]
if not bool(urlparse(password_url).netloc):
    password_url = singlekey_host + password_url

r = session.get(password_url, allow_redirects=False)
soup = BeautifulSoup(r.text, "html.parser")
requestVerificationToken = soup.find("input", {"name": "__RequestVerificationToken"}).get("value")

r = session.post(
    password_url,
    data={
        "Password": password,
        "RememberMe": "false",
        "__RequestVerificationToken": requestVerificationToken,
    },
    allow_redirects=False,
)

while True:
    if return_url.startswith("/"):
        return_url = singlekey_host + return_url
    r = session.get(return_url, allow_redirects=False)
    debug(f"{return_url=}, {r} {r.text}")
    if r.status_code != 302:
        break
    return_url = r.headers["location"]
    if return_url.startswith("hcauth://"):
        break
debug(f"{return_url=}")

debug("--------")

url = urlparse(return_url)
query = parse_qs(url.query)

if query.get("ReturnUrl") is not None:
    print("Wrong credentials.")
    print(
        "If you forgot your login/password, you can restore them by opening "
        "https://singlekey-id.com/auth/en-us/login in browser"
    )
    exit(1)

code = query.get("code")[0]
state = query.get("state")[0]
grant_type = query.get("grant_type")[0]  # "authorization_code"

debug(f"{code=} {grant_type=} {state=}")

auth_url = base_url + "login"
token_url = base_url + "token"

token_fields = {
    "grant_type": grant_type,
    "client_id": app_id,
    "code_verifier": verifier,
    "code": code,
    "redirect_uri": login_query["redirect_uri"],
}

debug(f"{token_url=} {token_fields=}")

r = requests.post(token_url, data=token_fields, allow_redirects=False)
if r.status_code != requests.codes.ok:
    print("Bad code?", file=sys.stderr)
    print(r.headers, r.text)
    exit(1)

debug("--------- got token page ----------")

token = json.loads(r.text)["access_token"]
debug(f"Received access {token=}")

headers = {
    "Authorization": "Bearer " + token,
}


# Try to request account details from all geos. Whichever works, we'll use next.
for asset_url in asset_urls:
    r = requests.get(asset_url + "account/details", headers=headers)
    if r.status_code == requests.codes.ok:
        break

# now we can fetch the rest of the account info
if r.status_code != requests.codes.ok:
    print("unable to fetch account details", file=sys.stderr)
    print(r.headers, r.text)
    exit(1)

# print(r.text)
account = json.loads(r.text)
configs = []

print(account, file=sys.stderr)

for app in account["data"]["homeAppliances"]:
    app_brand = app["brand"]
    app_type = app["type"]
    app_id = app["identifier"]

    config = {
        "name": app_type.lower(),
    }

    configs.append(config)

    if "tls" in app:
        # fancy machine with TLS support
        config["host"] = app_brand + "-" + app_type + "-" + app_id
        config["key"] = app["tls"]["key"]
    else:
        # less fancy machine with HTTP support
        config["host"] = app_id
        config["key"] = app["aes"]["key"]
        config["iv"] = app["aes"]["iv"]

    # Fetch the XML zip file for this device
    app_url = asset_url + "api/iddf/v1/iddf/" + app_id
    print("fetching", app_url, file=sys.stderr)
    r = requests.get(app_url, headers=headers)
    if r.status_code != requests.codes.ok:
        print(app_id, ": unable to fetch machine description?")
        next

    # we now have a zip file with XML, let's unpack them
    content = r.content
    print(app_url + ": " + app_id + ".zip", file=sys.stderr)
    with open(app_id + ".zip", "wb") as f:
        f.write(content)
    z = ZipFile(io.BytesIO(content))
    # print(z.infolist())
    features = z.open(app_id + "_FeatureMapping.xml").read()
    description = z.open(app_id + "_DeviceDescription.xml").read()

    machine = xml2json(features, description)
    config["description"] = machine["description"]
    config["features"] = augment_device_features(machine["features"])

print(json.dumps(configs, indent=4))
