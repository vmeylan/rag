import concurrent.futures
import logging
import os
from functools import partial
from typing import Union, Callable
from urllib.parse import urlparse

import pandas as pd

from datasets.evaluation_data.site_configs import site_configs
from src.Llama_index_sandbox.custom_pymupdfreader.base import PyMuPDFReader
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.Llama_index_sandbox import root_dir, ETHGLOBAL_DOCS
from src.Llama_index_sandbox.constants import *
from src.Llama_index_sandbox.data_ingestion_pdf.utils import sanitize_metadata_value, check_file_exclusion
from src.Llama_index_sandbox.utils.utils import timeit, save_successful_load_to_csv, compute_new_entries


def load_single_pdf(file_path, existing_metadata: pd.DataFrame, current_df, loader=PyMuPDFReader(), debug=False):
    try:
        parent_dir_name = os.path.basename(os.path.dirname(file_path))
        filename = os.path.basename(file_path)

        # Ensure comparison strings are not empty or NaN
        existing_metadata = existing_metadata.dropna(subset=['document_name', 'pdf_link'])

        # Create a copy of the DataFrame to safely add a new column without affecting the original DataFrame
        existing_metadata = existing_metadata.copy()

        # Now add the new column to the copy of the DataFrame
        existing_metadata['extracted_domain'] = existing_metadata['pdf_link'].apply(lambda x: urlparse(x).netloc if pd.notnull(x) else None)

        # First, match on filename to potentially reduce the number of comparisons
        matching_filenames = existing_metadata[existing_metadata['document_name'].str.contains(filename, na=False)]

        # Then, further refine by matching the extracted domain with parent_dir_name
        matching_entries = matching_filenames[matching_filenames['extracted_domain'] == parent_dir_name]

        # is_in_current_df = not current_df[current_df['document_name'] == filename].empty
        # is_in_existing_metadata = not matching_entries.empty

        # if is_in_current_df or is_in_existing_metadata:
        #     logging.info(f"Skipping file [{filename}] as it is already processed")
        #     return [], {}

        # If no matching entry is found in existing_metadata, log a warning
        if not matching_entries.empty:
            documents_details = matching_entries.to_dict('records')[0]
            doc_title = documents_details.get('title', filename.replace('.pdf', '').replace('-', ' '))
            title = sanitize_metadata_value(doc_title)
            extracted_author = sanitize_metadata_value(documents_details['authors'])
            link = sanitize_metadata_value(documents_details['pdf_link'])
            extracted_release_date = sanitize_metadata_value(documents_details['release_date'])
        else:
            title = filename.replace('.pdf', '').replace('-', ' ')
            link, extracted_author, extracted_release_date = '', '', ''
            logging.warning(f"Couldn't find matching entry for [{file_path}] in existing metadata")

        documents = loader.load(file_path=file_path)

        for document in documents:
            if 'file_path' in document.metadata.keys():
                del document.metadata['file_path']

            if document.text is None or document.text == '':
                logging.error(f"Empty content in {title} with {file_path}")

            document.metadata.update({
                'document_type': DOCUMENT_TYPES.ARTICLE.value,
                'title': title,
                'authors': extracted_author,
                'pdf_link': link,
                'release_date': extracted_release_date
            })

        if debug:
            logging.info(f'Processed [{filename}] with named [{title}]')
        documents_details = {
            'title': title,
            'authors': extracted_author,
            'pdf_link': link,
            'release_date': extracted_release_date,
            'document_name': filename
        }
        # Save the successful load details to CSV
        save_successful_load_to_csv(documents_details, csv_filename='docs.csv', fieldnames=['title', 'authors', 'pdf_link', 'release_date', 'document_name'])
        return documents, documents_details
    except Exception as e:
        logging.error(f"Failed to load {file_path}: {e}")
        return [], {}


@timeit
def load_pdfs(directory_path: Union[str, Path], files, existing_metadata: pd.DataFrame, current_df: pd.DataFrame, domain: str, num_files: int = None, files_window = None, num_cpus: int = None, debug=False):
    if not isinstance(directory_path, Path):
        directory_path = Path(directory_path)

    if num_files is not None:
        files = files[:num_files]

    all_documents = []
    partial_load_single_pdf = partial(load_single_pdf, existing_metadata=existing_metadata, current_df=current_df, debug=debug)

    pdf_loaded_count = 0  # Initialize the counter
    # Initialize a list to accumulate metadata
    metadata_accumulator = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_cpus) as executor:
        futures = {executor.submit(partial_load_single_pdf, file_path=pdf_file): pdf_file for pdf_file in files}

        for future in concurrent.futures.as_completed(futures):
            pdf_file = futures[future]
            try:
                documents, documents_details = future.result()
                if not documents:
                    continue
                if documents_details['title'] not in [md['title'] for md in metadata_accumulator]:
                    metadata_accumulator.append(documents_details)
                    pdf_loaded_count += 1

                all_documents.extend(documents)
                pdf_loaded_count += 1  # Increment the counter for each successful load
            except Exception as e:
                logging.info(f"Failed to process {pdf_file}, with reason: {e}")
                # os.remove(pdf_file)

    logging.info(f"Successfully loaded [{pdf_loaded_count}] documents, for all_documents length [{len(all_documents)}], from [{domain}].")
    return all_documents, metadata_accumulator


@timeit
def load_docs_as_pdf(debug=False, overwrite=False, num_files: int = None, files_window=None, num_cpus: int = None):
    overwrite = True

    all_docs = []
    csv_path = os.path.join(root_dir, 'datasets/evaluation_data/docs_details.csv')
    current_df = pd.read_csv(f"{root_dir}/pipeline_storage/docs.csv") if not overwrite else pd.DataFrame(columns=['title', 'authors', 'pdf_link', 'release_date', 'document_name'])
    existing_metadata = pd.read_csv(csv_path) if os.path.exists(csv_path) else pd.DataFrame(columns=['title', 'authors', 'pdf_link', 'release_date', 'document_name'])

    for name, config in site_configs.items():
        base_url = config.get('base_url')
        domain = urlparse(base_url).netloc
        directory_path = os.path.join(root_dir, ETHGLOBAL_DOCS, domain)

        files = list(Path(directory_path).rglob("*.pdf"))  # Use rglob for recursive search if needed
        if num_files is not None:
            files = files[:num_files]

        logging.info(f"Processing directory: [{directory_path}] with [{len(files)}] files")
        all_documents, all_documents_details = load_pdfs(directory_path, files=files, existing_metadata=existing_metadata, current_df=current_df, domain=domain, num_files=num_files, num_cpus=num_cpus, debug=debug)
        all_docs.extend(all_documents)

    logging.info(f"Successfully loaded [{len(all_docs)}] documents from all domains.")
    return all_docs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    num_cpus = 1  # os.cpu_count()# 1
    load_docs_as_pdf(debug=True, num_cpus=num_cpus, overwrite=True)
