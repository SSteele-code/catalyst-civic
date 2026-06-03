import unittest
from pathlib import Path
from ingest import Ingestor

class TestIngest(unittest.TestCase):
    def setUp(self):
        # We don't need a real staging dir for unit tests of logic
        self.ingestor = Ingestor(Path("fake.vtt"), Path("temp_staging"))

    def test_clean_artifacts(self):
        text = "Hello [Music] world [Applause]"
        cleaned = self.ingestor.clean_artifacts(text)
        self.assertEqual(cleaned, "Hello world")

    def test_reconstruct_sentences_basic(self):
        text = "thank you mr mayor I make a motion to approve the agenda"
        reconstructed = self.ingestor.reconstruct_sentences(text)
        # We expect capitalization and periods
        self.assertIn("Thank you", reconstructed)
        self.assertIn("Mr Mayor", reconstructed)
        # Note: current implementation is weak, we'll improve it

    def test_detect_generation(self):
        gen3 = ">> Hello everyone"
        self.assertEqual(self.ingestor.detect_generation(gen3), 3)
        
        gen2 = "Hello everyone. This is a meeting."
        self.assertEqual(self.ingestor.detect_generation(gen2), 2)
        
        gen1 = "hello everyone this is a meeting"
        self.assertEqual(self.ingestor.detect_generation(gen1), 1)

    def test_artifact_header_removal(self):
        # Test that "Kind: captions" and other metadata are gone
        text = "Kind: captions Language: en WEBVTT hello"
        # This isn't currently in a separate method, let's refactor ingest.py
        pass

if __name__ == "__main__":
    unittest.main()
