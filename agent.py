from dotenv import load_dotenv
from agents import Agent, Runner

load_dotenv()

agent = Agent(
    name="Assistant",
    instructions="You are a helpful general-purpose assistant.",
)

prompt = input("You: ")
result = Runner.run_sync(agent, prompt)
print(f"Assistant: {result.final_output}")
