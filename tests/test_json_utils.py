import unittest

from geng_agent.json_utils import parse_json_object


class JsonUtilsTests(unittest.TestCase):
    def test_strict_parser_rejects_unescaped_manifest(self) -> None:
        raw = '''{
  "files": [
    {
      "path": "README.md",
      "content": "# Title\\n**"Quoted Paper"**\\n"
    },
    {
      "path": "run_experiment.py",
      "content": "print("hello")\\n"
    }
  ]
}'''

        with self.assertRaises(Exception):
            parse_json_object(raw)

    def test_explicit_loose_parser_recovers_file_manifest_with_unescaped_quotes(self) -> None:
        raw = '''{
  "files": [
    {
      "path": "README.md",
      "content": "# Title\\n**"Quoted Paper"**\\n"
    },
    {
      "path": "run_experiment.py",
      "content": "print("hello")\\n"
    }
  ]
}'''

        parsed = parse_json_object(raw, allow_loose_manifest=True)

        self.assertEqual(parsed["files"][0]["path"], "README.md")
        self.assertIn('"Quoted Paper"', parsed["files"][0]["content"])
        self.assertIn('print("hello")', parsed["files"][1]["content"])
        self.assertTrue(parsed["_meta"]["loose_recovery_used"])

    def test_parser_accepts_minimax_think_block_before_json_fence(self) -> None:
        raw = """<think>
I will reason before answering.
</think>
```json
{
  "paper_domain": "communication",
  "engineering_facts": [],
  "missing_information": []
}
```"""

        parsed = parse_json_object(raw)

        self.assertEqual(parsed["paper_domain"], "communication")
        self.assertEqual(parsed["engineering_facts"], [])


if __name__ == "__main__":
    unittest.main()
