# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import unittest
from aiopslab.orchestrator.parser import ResponseParser
from aiopslab.utils.status import ResponseParsingError


class TestSubmitParser(unittest.TestCase):
    def test_detection_submit(self):
        input_1 = """
        Action:
        ```
        submit('yes')
        ```
        """
        parser = ResponseParser()
        parsed = parser.parse(input_1)
        self.assertEqual(parsed["api_name"], "submit")
        self.assertEqual(parsed["args"], ["yes"])
        self.assertEqual(parsed["kwargs"], {})

        input_2 = """
        Action:
        ```
        submit('no')
        ```
        """
        parser = ResponseParser()
        parsed = parser.parse(input_2)
        self.assertEqual(parsed["api_name"], "submit")
        self.assertEqual(parsed["args"], ["no"])
        self.assertEqual(parsed["kwargs"], {})

    def test_detection_submit_with_kwargs(self):
        input = """
        Action:
        ```
        submit(has_anomaly='yes')
        ```
        """
        parser = ResponseParser()
        parsed = parser.parse(input)
        self.assertEqual(parsed["api_name"], "submit")
        self.assertEqual(parsed["args"], [])
        self.assertEqual(parsed["kwargs"], {"has_anomaly": "yes"})

    def test_localization_submit(self):
        input = """
        Action:
        ```
        submit(['service1', 'service2'])
        ```
        """
        parser = ResponseParser()
        parsed = parser.parse(input)
        self.assertEqual(parsed["api_name"], "submit")
        self.assertEqual(parsed["args"], [["service1", "service2"]])
        self.assertEqual(parsed["kwargs"], {})

    def test_localization_submit_with_kwargs(self):
        input = """
        Action:
        ```
        submit(faulty_components=['s1', 's2', 's3'])
        ```
        """
        parser = ResponseParser()
        parsed = parser.parse(input)
        self.assertEqual(parsed["api_name"], "submit")
        self.assertEqual(parsed["args"], [])
        self.assertEqual(parsed["kwargs"], {"faulty_components": ["s1", "s2", "s3"]})

    def test_analysis_submit(self):
        input = """
        Action:
        ```
        submit({'system_level': 'xxx', 'fault_type': 'yyy'})
        ```
        """
        parser = ResponseParser()
        parsed = parser.parse(input)
        self.assertEqual(parsed["api_name"], "submit")
        self.assertEqual(parsed["args"], [{"system_level": "xxx", "fault_type": "yyy"}])
        self.assertEqual(parsed["kwargs"], {})

    def test_analysis_submit_with_kwargs(self):
        input = """
        Action:
        ```
        submit(analysis={'system_level': 'xxx', 'fault_type': 'yyy'})
        ```
        """
        parser = ResponseParser()
        parsed = parser.parse(input)
        self.assertEqual(parsed["api_name"], "submit")
        self.assertEqual(parsed["args"], [])
        self.assertEqual(
            parsed["kwargs"], {"analysis": {"system_level": "xxx", "fault_type": "yyy"}}
        )

    def test_mitigation_submit_no_args(self):
        input = """
        Action:
        ```
        submit()
        ```
        """
        parser = ResponseParser()
        parsed = parser.parse(input)
        self.assertEqual(parsed["api_name"], "submit")
        self.assertEqual(parsed["args"], [])
        self.assertEqual(parsed["kwargs"], {})


if __name__ == "__main__":
    unittest.main()
