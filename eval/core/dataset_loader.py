import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class BenchmarkDatasetLoader:
    """
    Loads and validates the retrieval benchmark dataset.
    Binds annotated target chunks to actual database DocumentChunk records.
    """

    def __init__(self, dataset_path: Optional[Path] = None, fixtures_dir: Optional[Path] = None):
        base_dir = Path(__file__).resolve().parent.parent
        self.dataset_path = dataset_path or (base_dir / "datasets" / "benchmark_v1.jsonl")
        self.fixtures_dir = fixtures_dir or (base_dir / "datasets" / "fixtures")

    def compute_fixture_hashes(self) -> Dict[str, str]:
        """Compute SHA-256 hashes of all fixture files to ensure dataset version integrity."""
        hashes = {}
        if not self.fixtures_dir.exists():
            return hashes

        for file_path in sorted(self.fixtures_dir.glob("*.md")):
            with open(file_path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
                hashes[file_path.name] = digest
        return hashes

    def load_samples(self) -> List[Dict[str, Any]]:
        """Load and parse JSONL benchmark samples."""
        if not self.dataset_path.exists():
            raise FileNotFoundError(f"Benchmark dataset not found at {self.dataset_path}")

        samples = []
        with open(self.dataset_path, "r", encoding="utf-8") as f:
            for line_idx, line in enumerate(f, start=1):
                clean_line = line.strip()
                if not clean_line:
                    continue
                try:
                    data = json.loads(clean_line)
                    samples.append(data)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON at line {line_idx} in {self.dataset_path}: {exc}")
        return samples

    @staticmethod
    def bind_samples_to_chunks(
        samples: List[Dict[str, Any]],
        chunk_lookup: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """
        Binds target chunks in the benchmark samples to real database chunk IDs.
        chunk_lookup: dict mapping "{doc_name}:{page_number}:{chunk_index}" -> db_chunk_id.
        """
        bound_samples = []
        for s in samples:
            sample_copy = dict(s)
            golden_map: Dict[str, float] = {}
            resolved_targets: List[Dict[str, Any]] = []

            for target in s.get("target_chunks", []):
                doc_name = target["doc_name"]
                page = target["page_number"]
                chunk_idx = target["chunk_index"]
                relevance = target.get("relevance", 2)

                lookup_key = f"{doc_name}:{page}:{chunk_idx}"
                db_chunk_id = chunk_lookup.get(lookup_key)

                if not db_chunk_id:
                    raise ValueError(
                        f"Target chunk '{lookup_key}' for query {s.get('id')} could not be found in ingested database!"
                    )

                golden_map[db_chunk_id] = float(relevance)
                resolved_target = dict(target)
                resolved_target["chunk_id"] = db_chunk_id
                resolved_targets.append(resolved_target)

            sample_copy["golden_map"] = golden_map
            sample_copy["golden_ids"] = list(golden_map.keys())
            sample_copy["resolved_targets"] = resolved_targets
            bound_samples.append(sample_copy)

        return bound_samples
