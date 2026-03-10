"""
Secure Token Scanner with contract safety analysis and liquidity validation.
Implements progressive filtering and anti-rug-pull heuristics.
"""
import asyncio
import json
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import logging
from web3 import Web3
from web3.contract import Contract
import requests
import pandas as pd
import numpy as np
from moralis import evm_api

from firebase_setup import get_state_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ContractSafetyAnalyzer:
    """Static contract analysis for common scam patterns."""
    
    RED_FLAG_FUNCTIONS = {
        'approve': ['max', 'unlimited', 'type(uint256).max'],
        'transfer': ['owner', 'fee'],
        'mint': ['owner', 'dev', 'team'],
        'setFee': ['any', 'high'],
        'blacklist': ['owner']
    }
    
    @staticmethod
    def analyze_contract_abi(abi: list) -> Dict[str, Any]:
        """Analyze contract ABI for suspicious functions."""
        findings = {
            'has_hidden_mint': False,
            'has_owner_blacklist': False,
            'has_fee_change': False,
            'is_verified': False,
            'function_count': 0,
            'suspicious_functions': []
        }
        
        try:
            findings['function_count'] = len([item for item in abi if item.get('type') == 'function'])
            
            for item in abi:
                if item.get('type') != 'function':
                    continue
                    
                func_name = item.get('name', '').lower()
                
                # Check for suspicious function patterns
                for flag_func, keywords in ContractSafetyAnalyzer.RED_FLAG_FUNCTIONS.items():
                    if flag_func in func_name:
                        findings['suspicious_functions'].append(func_name)
                        
                        if 'mint' in flag_func and 'owner' in func_name:
                            findings['has_hidden_mint'] = True
                        elif 'blacklist' in flag_func:
                            findings['has_owner_blacklist'] = True
                        elif 'fee' in flag_func and 'set' in func_name:
                            findings['has_fee_change'] = True
            
            findings['risk_score'] = (
                findings['has_hidden_mint'] * 0.4 +
                findings['has_owner_blacklist'] * 0.3 +
                findings['has_fee_change'] * 0.2 +
                (len(findings['suspicious_functions']) / max(findings['function_count'], 1)) * 0.1
            )
            
        except Exception as e:
            logger.error(f"ABI analysis failed: {e}")
            findings['analysis_error'] = str(e)
            
        return findings

class TokenScanner:
    """Main scanner service with progressive filtering."""
    
    def __init__(self, config_path: str = "config.yaml"):
        """Initialize scanner with configuration."""
        import yaml
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)
        
        self.state = get_state_manager()
        self.w3 = Web3(Web3.HTTPProvider(self.config['blockchain']['rpc_url']))
        self.moralis_api_key = self.config['blockchain']['moralis_api_key']
        self.polygonscan_key = self.config['blockchain']['polygonscan_api_key']
        
        # Cache for verified contracts
        self.verified_cache = {}
        self.blacklisted_tokens = set()
        
        logger.info("TokenScanner initialized")
    
    async def scan_new_contracts(self) -> List[str]:
        """Scan for new token contracts via Moralis Streams."""
        try:
            # Moralis API call for new contracts
            params = {
                "chain": "polygon",
                "from_block": int(self.w3.eth.block_number) - 100,  # Last 100 blocks
                "to_block": "latest",
            }
            
            result = evm_api.events.get_contract_logs(
                api_key=self.moralis_api_key,
                params=params,
            )
            
            contracts = []
            for log in result.get('result', []):
                address = log.get('address', '').lower()
                if address and address not in self.verified_cache:
                    contracts.append(address)
            
            logger.info(f"Found {len(contracts)} new contract addresses")
            return contracts
            
        except Exception as e:
            logger.error(f"Moralis scan failed: {e}")
            return []
    
    async def analyze_contract(self, address: str) -> Optional[Dict[str, Any]]:
        """Full contract analysis pipeline."""
        if address in self.blacklisted_tokens:
            return None
        
        # Step 1: Check verification status
        is_verified = await self._check_verification_status(address)
        if not is_verified:
            logger.warning(f"Contract {address} not verified - skipping")
            return None
        
        # Step 2: Get and analyze ABI
        abi = await self._get_contract_abi(address)
        if not abi:
            return None
        
        safety_analysis = ContractSafetyAnalyzer.analyze_contract_abi(abi)
        if safety_analysis.get('risk_score', 1) > 0.7:
            logger.warning(f"Contract {address} failed safety check: {safety_analysis}")
            self.blacklisted_tokens.add(address)
            return None
        
        # Step 3: Analyze liquidity and holders
        liquidity_data = await self._analyze_liquidity(address)
        holder_data = await self._analyze_holders(address)
        
        if not liquidity_data or not holder_data:
            return None
        
        # Step 4: Compile qualification data
        token_data = {
            'address': address,
            'timestamp': datetime.utcnow().isoformat(),
            'safety_analysis': safety_analysis,
            'liquidity': liquidity_data,
            'holders': holder_data,
            'contract_age_hours': await self._get_contract_age(address),
            'verification_status': 'verified'
        }
        
        # Calculate initial qualification score (excluding social signals)
        qualification_score = self._calculate_initial_score(token_data)
        token_data['qualification_score'] = qualification_score
        
        # Log to Firestore
        self.state.log_event('contract_analysis', 'contract_scanned', token_data)
        
        if qualification_score >= 0.4:  # Base threshold before social signals
            self.state.log_event('scan_queue', 'qualified_contract', {
                'address': address,
                'score': qualification_score,
                'analysis_summary': safety_analysis
            })
            logger.info(f"Contract {address} qualified with score {qualification_score:.2f}")
        
        return token_data
    
    async def _check_verification_status(self, address: str) -> bool:
        """Check if contract is verified on Polygonscan."""
        try:
            if address in self.verified_cache:
                return self.verified_cache[address]
            
            url = f"https://api.polygonscan.com/api"
            params = {
                'module': 'contract',
                'action': 'getsourcecode',
                'address': address,
                'apikey': self.polygonscan_key
            }
            
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            
            if data['status'] == '1':
                source_code = data['result'][0].get('SourceCode', '')
                is_verified = source_code != ''
                self.verified_cache[address] = is_verified
                return is_verified
            
            return False
            
        except Exception as e:
            logger.error(f"Verification check failed for {address}: {e}")
            return False
    
    async def _get_contract_abi(self, address: str) -> Optional[list]:
        """Retrieve contract ABI from Polygonscan."""
        try:
            url = f"https://api.polygonscan.com/api"
            params = {
                'module': 'contract',
                'action': 'getabi',
                'address': address,
                'apikey': self.polygonscan_key
            }
            
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            
            if data['status'] == '1' and data['result'] != 'Contract source code not verified':
                return json.loads(data['result'])
            
            return None
            
        except Exception as e:
            logger.error(f"ABI retrieval failed for {address}: {e}")
            return None
    
    async def _analyze_liquidity(self, address: str) -> Optional[Dict[str, Any]]:
        """Analyze token liquidity