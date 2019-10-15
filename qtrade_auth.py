import requests
import requests.auth
import base64
import time
import json
import binascii
from hashlib import sha256
from urllib.parse import urlparse

class QtradeAuth(requests.auth.AuthBase):
    def __init__(self, key):
        self.key_id, self.key = key.split(":")

    def __call__(self, req):
        # modify and return the request
        timestamp = str(int(time.time()))
        url_obj = urlparse(req.url)

        request_details = req.method + "\n"
        request_details += url_obj.path + url_obj.params + "\n"
        request_details += timestamp + "\n"
        if req.body:
            if isinstance(req.body, str):
                request_details += req.body + "\n"
            else:
                request_details += req.body.decode('utf8') + "\n"
        else:
            request_details += "\n"
        request_details += self.key
        hsh = sha256(request_details.encode("utf8")).digest()
        signature = base64.b64encode(hsh)
        req.headers.update({
            "Authorization": "HMAC-SHA256 {}:{}".format(self.key_id, signature.decode("utf8")),
            "HMAC-Timestamp": timestamp
        })
        return req

if __name__ == "__main__":
    api = requests.Session()

    key = open("lpbot_hmac.txt", "r").read().strip()
    api.auth = QtradeAuth(key)
    
    # Make a call to API
    res = api.get('https://api.qtrade.io/v1/user/me').json()
    print(res)