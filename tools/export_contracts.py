"""Export real API and tool schemas without constructing or calling a model."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def build_contracts() -> dict:
    from app.main import app
    from app.agent.merchant_agent import MerchantAgent
    from app.agent.customer_agent import CustomerAgent
    tools = {}
    for role, cls in [('merchant', MerchantAgent), ('customer', CustomerAgent)]:
        # Toolkit factories do not load model credentials or instantiate an LLM.
        toolkit = cls._create_toolkit()
        definitions = []
        for schema in await toolkit.get_tool_schemas():
            name = schema.get('function', schema)['name']
            tool = toolkit.get_tool(name)
            if hasattr(tool, '__await__'):
                tool = await tool
            definitions.append({'name': name, 'read_only': tool.is_read_only, 'schema': schema})
        tools[role] = definitions
    return {'openapi': app.openapi(), 'tools': tools}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/contracts')
    args = parser.parse_args()
    contracts = asyncio.run(build_contracts())
    args.output.mkdir(parents=True, exist_ok=True)
    for name, contract in contracts.items():
        (args.output / f'{name}.json').write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Exported OpenAPI and tool contracts: {args.output}')


if __name__ == '__main__':
    main()
