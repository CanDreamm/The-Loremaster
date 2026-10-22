"""Official SDK schemas, independent of bot implementation and local HTTP fakes."""
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

CONTRACT = json.loads((Path(__file__).parent / 'fixtures' / 'postmypost-contract.json').read_text())


def validate_contract(model, value):
    Draft202012Validator(CONTRACT['schemas'][model], format_checker=FormatChecker()).validate(value)
