import urllib.request
def _management_headers(api_key: str):
    return {"Authorization": f"Bearer {api_key}", "User-Agent": "test"}

def test_delete(base_url, api_key, filename):
    target_url = f"{base_url}/v0/management/auth-files/{filename}"
    req = urllib.request.Request(url=target_url, method="DELETE", headers=_management_headers(api_key))
    try:
        with urllib.request.urlopen(req) as resp:
            print(resp.status, resp.read())
    except Exception as e:
        print("Error:", e)
print("done")
