from embeddings_dataset_langchain import EmbeddingsDataset
from transformers import AutoModelForCausalLM, BitsAndBytesConfig, AutoTokenizer, TextIteratorStreamer
from langchain_community.embeddings.huggingface import HuggingFaceEmbeddings
from threading import Thread
from zipfile import ZipFile
import PROMPTS
import os
import torch
import json
import git
import subprocess

import shutil

def supports_flash_attention():
    """Check if a GPU supports FlashAttention."""
    major, minor = torch.cuda.get_device_capability(0)
    
    # Check if the GPU architecture is Ampere (SM 8.x) or newer (SM 9.x)
    is_sm8x = major == 8 and minor >= 0
    is_sm9x = major == 9 and minor >= 0

    return is_sm8x or is_sm9x

class RetrivalAugment:
    
    def __init__(self, cache_repo_list = os.path.join(os.path.dirname(__file__), 'cached_repos.json'), cache_dir= os.path.join(os.path.dirname(__file__), 'py_cache')) -> None:
        # Embedding Model to be Used in Document and Querry Embeddings
        self.base_embedding_model = HuggingFaceEmbeddings(model_name='sentence-transformers/all-MiniLM-L6-v2', 
                                                          model_kwargs = {'device': 'cuda' if torch.cuda.is_available() else 'cpu'})
        
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        
        # Store requested cache list location
        self.cache_repo_list = cache_repo_list
        # Create cache list if not present, otherwise load it
        if not os.path.exists(self.cache_repo_list):
            self.cached = {'cached_repos': {}, 'cached_shared' : []}
            json.dump(self.cached, open(self.cache_repo_list, 'w+'), indent=6)
        else:
            self.cached = json.load(open(self.cache_repo_list, 'r'))
            
        
        # Sample device to be used by models
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        # Load Language Model to be used in RAG
        self.tokenizer  = AutoTokenizer.from_pretrained('mistralai/Mistral-7B-Instruct-v0.2')
        if torch.cuda.is_available():
            nf4_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.bfloat16
            )
            # Load Attention based on GPU params
            self.model  = AutoModelForCausalLM.from_pretrained('mistralai/Mistral-7B-Instruct-v0.2', quantization_config=nf4_config, device_map="auto", attn_implementation="flash_attention_2" if supports_flash_attention() else "sdpa" )
            
        else:
            self.model  = AutoModelForCausalLM.from_pretrained('mistralai/Mistral-7B-Instruct-v0.2',  device_map="auto").to(self.device)
        # Template variable for shared documents that could be uploaded
        self.shared_documents = {}
        # Load Cached Embeddings from cache list
        self.version_specific_documents = {}
        self.__load_all_cached()
    
    def __load_all_cached(self):
        # Load each git repo
        for key in self.cached['cached_repos'].keys():
            # Embedding storage variable
            self.version_specific_documents[key] = {}
            normalized_github_path = key.removesuffix('.git')
            _ ,repo_rel_name = os.path.split(normalized_github_path)
            # Load all cached branches of the git repo
            for branch in self.cached['cached_repos'][key]:
                self.version_specific_documents[key][branch] = EmbeddingsDataset(os.path.join(self.cache_dir ,repo_rel_name, branch), 
                                                                cache_dir=os.path.join(self.cache_dir , f'{repo_rel_name}-{branch}-embed'), 
                                                                transformer_model=self.base_embedding_model)
                
        for zip_name in self.cached['cached_shared']:
            self.shared_documents[zip_name] = EmbeddingsDataset(os.path.join(self.cache_dir, zip_name.removesuffix('.zip')), 
                                                    cache_dir=os.path.join(self.cache_dir, f'{zip_name.removesuffix(".zip")}-embed'), 
                                                    transformer_model=self.base_embedding_model)
      
    def _get_repo_branches(self, base_repo: str):
        # Check if proper git repo format
        if not base_repo.endswith('.git'):
            return []
        branches = []
        g = git.cmd.Git()
        # Try to lookup all of git branches
        try:
            for ref in g.ls_remote('--heads',base_repo).split('\n'):
                branches.append(ref.split('/')[-1])
            return branches
        except Exception as e:
            print(e)
            return []
         
    def _get_cached_repos(self):
        # Return all cached repos
        return list(self.cached['cached_repos'].keys())
    
    def _get_cached_shared(self):
        # Return all caches secondary directories
        return list(self.cached['cached_shared'])
      
    def _check_branch_cache(self, base_repo: str):
        # Return all cached branches to given repo
        if not base_repo.endswith('.git'):
            return []
        if base_repo in self.cached['cached_repos'].keys():
            return self.cached['cached_repos'][base_repo]
        else:
            return []
            
    def _add_following_repo_branches(self, base_repo:str, repo_branches: list[str]):
        if not base_repo.endswith('.git'):
            return
        normalized_github_path = base_repo.removesuffix('.git')
        _ ,repo_rel_name = os.path.split(normalized_github_path)
        if not base_repo in self.cached['cached_repos'].keys():
            self.cached['cached_repos'][base_repo] = []
        if not base_repo in self.version_specific_documents.keys():
            self.version_specific_documents[base_repo] = {}
        for requested_branch in repo_branches:
            if requested_branch in self.cached['cached_repos'][base_repo]:
                if not requested_branch in self.version_specific_documents[base_repo].keys():
                    self.version_specific_documents[base_repo][requested_branch] = EmbeddingsDataset(os.path.join(self.cache_dir , repo_rel_name, requested_branch), 
                                                                                      cache_dir=os.path.join(self.cache_dir , f'{repo_rel_name}-{requested_branch}-embed'), 
                                                                                      transformer_model=self.base_embedding_model)
            else:
                subprocess.run(f'curl -L -o {os.path.abspath(os.path.join(self.cache_dir , requested_branch + ".zip"))} {normalized_github_path}/zipball/{requested_branch}', shell=True)
                if os.path.exists(os.path.join(self.cache_dir , f'{requested_branch}.zip')):
                    try:
                        zf = ZipFile(os.path.join(self.cache_dir , f'{requested_branch}.zip'), 'r') 
                        
                        os.makedirs(os.path.join(self.cache_dir , repo_rel_name), exist_ok=True)
                        if os.path.exists(os.path.join(self.cache_dir , repo_rel_name, requested_branch)):
                            shutil.rmtree(os.path.join(self.cache_dir , repo_rel_name, requested_branch))
                        
                        filenames = list(filter(lambda x: x.endswith('.txt') or x.endswith('.md') or x.endswith('.rst'), zf.namelist()) )

                        zf.extractall(os.path.join(self.cache_dir , repo_rel_name), members=filenames)
                        shutil.move(os.path.join(self.cache_dir , repo_rel_name, zf.namelist()[0]),
                                  os.path.join(self.cache_dir , repo_rel_name, requested_branch))
                        if os.path.exists(os.path.join(self.cache_dir , f'{repo_rel_name}-{requested_branch}-embed')):
                            shutil.rmtree(os.path.join(self.cache_dir , f'{repo_rel_name}-{requested_branch}-embed'))
                        
                        self.version_specific_documents[base_repo][requested_branch] = EmbeddingsDataset(os.path.join(self.cache_dir , repo_rel_name, requested_branch), 
                                                                                          cache_dir=os.path.join(self.cache_dir , f'{repo_rel_name}-{requested_branch}-embed'), 
                                                                                          transformer_model=self.base_embedding_model)
                        self.cached['cached_repos'][base_repo].append(requested_branch)
                        zf.close()
                        os.remove(os.path.join(self.cache_dir , f'{requested_branch}.zip'))
                    except Exception as e:
                        print(e)
                        os.remove(os.path.join(self.cache_dir , f'{requested_branch}.zip'))
        # Remove Key entry if embedding failed
        if len(self.cached['cached_repos'][base_repo]) == 0:
            self.cached['cached_repos'].pop(base_repo)
            self.version_specific_documents.pop(base_repo)
        
        json.dump(self.cached, open(self.cache_repo_list, 'w+'), indent=6)
        
    def _add_following_zip(self, zip_info:str):
        _, zip_name = os.path.split(zip_info)
        if not zip_name.endswith('.zip') or zip_name in self.cached['cached_shared']:
            os.remove(zip_info)
            return
        else:
            shutil.move(zip_info, os.path.join(self.cache_dir , zip_name))
            try:
                zf = ZipFile(os.path.join(self.cache_dir , zip_name), 'r') 
                
                if os.path.exists(os.path.join(self.cache_dir , zip_name.removesuffix('.zip'))):
                    shutil.rmtree(os.path.join(self.cache_dir , zip_name.removesuffix('.zip')))
                    
                os.makedirs(os.path.join(self.cache_dir , zip_name.removesuffix('.zip')), exist_ok=True)
                    
                filenames = list(filter(lambda x: x.endswith('.txt') or x.endswith('.md') or x.endswith('.rst'), zf.namelist()) )
                zf.extractall(os.path.join(self.cache_dir , zip_name.removesuffix('.zip')), members=filenames)
                
                if os.path.exists(os.path.join(self.cache_dir , f'{zip_name.removesuffix(".zip")}-embed')):
                    shutil.rmtree(os.path.join(self.cache_dir , f'{zip_name.removesuffix(".zip")}-embed'))
                    
                self.shared_documents[zip_name] = EmbeddingsDataset(os.path.join(self.cache_dir , zip_name.removesuffix('.zip')), 
                                                    cache_dir=os.path.join(self.cache_dir , f'{zip_name.removesuffix(".zip")}-embed'), 
                                                    transformer_model=self.base_embedding_model)
                self.cached['cached_shared'].append(zip_name)
                zf.close()
                os.remove(os.path.join(self.cache_dir , zip_name))   
            except Exception as e:
                print(e)
                os.remove(os.path.join(self.cache_dir , zip_name))
                
        json.dump(self.cached, open(self.cache_repo_list, 'w+'), indent=6)
        
    def _get_relevant_docs(self, git_repo, versions, inputs):
        result_string = "### Most Relevant Documents"
        for version in versions:
            relevant_docs = (self.version_specific_documents[git_repo][version]).relevant_docs_filename(inputs, 
                                                                                                        k=max(1, 5//len(versions)), fetch_k=max(1, 30//len(versions)))
            full_paths = []
            for path in relevant_docs:
                _, filename = os.path.split(path)
                rel_file_path:str = path.split(version)[-1].replace(os.sep, '/')
                rel_file_path_norm = rel_file_path.removeprefix('/')
                repo_name = git_repo.removesuffix('.git')
                full_paths.append(f'[{rel_file_path_norm}]({repo_name}/blob/{version}{rel_file_path})' )
            full_paths = sorted(full_paths)
            result_string += f'\n #### Branch {version} \n' + '  \n'.join(full_paths)
            
        return result_string
                           
            
    def __call__(self, git_repo, versions= None, inputs = '', shared=None):
        if len(self.version_specific_documents.keys()) == 0 and len(self.shared_documents.keys()) == 0:
            return 'I was not given any documents from which to answer.'
        
        streamer = TextIteratorStreamer(self.tokenizer, skip_prompt=True, skip_special_tokens=True)
        version_context = []
        for version in versions:
            version_context += self.version_specific_documents[git_repo][version](inputs, 
                                                                                  k=max(1, 5//len(versions)), fetch_k=max(1, 30//len(versions)))
         
        shared_context = []
        for share in shared:
            shared_context += self.shared_documents[share].querry_documents(f"{'' if (versions==None or len(versions) == 0)  else versions}\n{inputs}", 
                                                                            k=max(1, 3//len(shared)), fetch_k=max(1, 20//len(shared)))
        
        messages = [
            {
                "role": "user",
                "content": PROMPTS.SYSTEM_PROMPT_FAST + PROMPTS.INPUT_PROMPT_FAST.format(version=versions, 
                                                                               version_context=version_context, 
                                                                               shared_context=shared_context, 
                                                                               inputs=inputs)
            },

        ]
        
        
            
        chatted = self.tokenizer.apply_chat_template(messages, return_tensors="pt", return_dict=True).to(self.device)
        generate_kwargs = dict(
            chatted,
            streamer=streamer,
            max_new_tokens=2048,
            do_sample=True,
            top_p=0.99,
            top_k=500,
            temperature=0.2,
            num_beams=1,
        )
        t = Thread(target=self.model.generate, kwargs=generate_kwargs) 
        t.start()
        partial_message = ""
        for new_token in streamer:
            partial_message += new_token
            yield partial_message

    
if __name__ == '__main__':
    augment = RetrivalAugment()
    augment._add_following_repo_branches('https://github.com/jinymusim/GPT-Czech-Poet.git', ['main'])
    print(augment._get_cached_repos())
    print(augment._check_branch_cache('https://github.com/jinymusim/GPT-Czech-Poet.git'))