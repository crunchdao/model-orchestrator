import io
import json
import os
import urllib
import urllib.parse
from typing import Tuple, Union, IO

import requests
import urllib3.util

from ..utils.logging_utils import get_logger

logger = get_logger()


class NotebookUnavailableError(ValueError):
    pass


SourceType = Union[str, bytes]


def load_notebook(source: SourceType) -> Tuple[dict, str]:
    try:
        # Case 1: URL or local path (string)
        if isinstance(source, str):
            if source.startswith(("http://", "https://")):
                url = urllib3.util.parse_url(source)

                # GitHub → raw
                if url.host == "github.com":
                    query = urllib.parse.parse_qs(url.query)
                    query["raw"] = ["true"]
                    url = url._replace(query=urllib.parse.urlencode(query, doseq=True))
                    source = url.url

                response = requests.get(source)
                response.raise_for_status()
                return response.json(), os.path.basename(url.path or "notebook.ipynb")

            # local path
            with open(source, "r", encoding="utf-8") as f:
                return json.load(f), os.path.basename(source)
        elif isinstance(source, bytes):
            data = json.load(io.BytesIO(source))
            return data, "notebook.ipynb"
    except Exception as exception:
        raise NotebookUnavailableError(f"Could not load notebook from {source}: {exception}") from exception

    raise ValueError(f"Unknown notebook source type: {type(source)}")



def import_notebook(notebook_source: SourceType, out_dir: str):
    notebook, _ = load_notebook(notebook_source)

    from crunch_convert.notebook import extract_from_cells
    from crunch_convert.requirements_txt import CrunchHubWhitelist, format_files_from_imported
    flatten = extract_from_cells(
        notebook.get("cells", []),
        print=logger.debug
    )

    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "notebook.ipynb"), "w") as fd:
        fd.write(json.dumps(notebook, indent=4))

    with open(os.path.join(out_dir, "main.py"), "w") as fd:
        fd.write(flatten.source_code)

    for embed_file in flatten.embedded_files:
        file_path = os.path.join(out_dir, embed_file.path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        with open(file_path, "w") as fd:
            fd.write(embed_file.content)

    whitelist = CrunchHubWhitelist()

    requirements_files = format_files_from_imported(
        flatten.requirements,
        header="extracted from a notebook",
        whitelist=whitelist,
    )
    real_content = ""
    for requirement_language, content in requirements_files.items():
        real_content += content

    with open(os.path.join(out_dir, "requirements.txt"), "w") as fd:
        fd.write(real_content)
