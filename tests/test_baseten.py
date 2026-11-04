import os
from openai import OpenAI

client = OpenAI(
    base_url="https://inference.baseten.co/v1",
    api_key=os.environ["BASETEN_API_KEY"],
)

response = client.chat.completions.create(
    model="meta-models/muse-spark-open-1026",
    messages=[{"role": "user", "content": "Reply with: Baseten connection works."}],
)

print(response.choices[0].message.content)
