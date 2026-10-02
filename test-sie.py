import os

from sie_sdk import SIEClient
from sie_sdk.types import Item

client = SIEClient(
    api_key=os.environ["SIE_API_KEY"],
    base_url=os.getenv("SIE_BASE_URL", "https://api.superlinked.com"),
    timeout_s=90,
)

result = client.encode(
    os.getenv("SIE_MODEL", "BAAI/bge-m3"),
    [
        Item(text="first document"),
        Item(text="second document"),
    ],
    output_types=["dense"],
    wait_for_capacity=True,
    provision_timeout_s=300,
)

print(len(result))
print(len(result[0]["dense"]))
