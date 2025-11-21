import json
import os
import urllib
import urllib.parse
from typing import Tuple

import requests
import urllib3.util


class NotebookUnavailableError(ValueError):
    pass


def load_notebook(path_or_url: str) -> Tuple[dict, str]:
    try:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = urllib3.util.parse_url(path_or_url)

            # Python, this is very ugly...
            if url.host == "github.com":
                query = urllib.parse.parse_qs(url.query)
                query["raw"] = ["true"]
                url = url._replace(query=urllib.parse.urlencode(query, doseq=True))
                path_or_url = url.url

            response = requests.get(path_or_url)
            response.raise_for_status()

            return response.json(), os.path.basename(url.path or "notebook.ipynb")
        else:
            with open(path_or_url, "r") as file:
                return json.load(file), os.path.basename(path_or_url)
    except Exception as exception:
        raise NotebookUnavailableError(f"Could not load notebook from {path_or_url}: {exception}") from exception
