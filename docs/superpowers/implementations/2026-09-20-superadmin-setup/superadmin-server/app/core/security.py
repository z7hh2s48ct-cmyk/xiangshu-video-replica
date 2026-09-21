"""Core security utilities for authentication and authorization.

This module provides essential security functions including:
- Password hashing with bcrypt
- JWT token creation and validation
- Fernet encryption/decryption for sensitive data

Security best practices:
- Never store plain-text passwords
- Use strong bcrypt cost factor (12+)
- Short-lived access tokens (15 min) + refresh tokens (7 days)
- Validate all token claims strictly
- Never log sensitive credentials

Author: AI Implementation Team
Date: 2026-09-20
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import jwt
from cryptography.fernet import Fernet
from jose import JWTError, jwt as pyjwt
from passlib.context import CryptContext

from app.config.settings import settings

logger = logging.getLogger(__name__)

# Bcrypt context for password hashing
pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12,  # Cost factor >= 12 recommended
)


class SecurityUtils:
    """Security utility class for password operations and token management."""
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt with auto-generated salt.
        
        Args:
            password: Plain-text password string to hash
            
        Returns:
            Bcrypt-hashed password string ($2b$... format)
            
        Examples:
            >>> hash = SecurityUtils.hash_password("SecurePassword123!")
            >>> assert hash.startswith("$2b$")
        """
        if not password or len(password) < 8:
            raise ValueError("Password must be at least 8 characters long")
        
        return pwd_context.hash(password)
    
    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify a plain-text password against a stored hash.
        
        Args:
            plain_password: User-provided plain-text password
            hashed_password: Stored bcrypt hash from database
            
        Returns:
            True if passwords match, False otherwise
            
        Examples:
            >>> hashed = SecurityUtils.hash_password("MyPass123!")
            >>> SecurityUtils.verify_password("MyPass123!", hashed)
            True
            >>> SecurityUtils.verify_password("WrongPass", hashed)
            False
        """
        try:
            return pwd_context.verify(plain_password, hashed_password)
        except Exception as e:
            logger.error(f"Password verification failed: {e}")
            return False
    
    @staticmethod
    def create_access_token(
        subject: str,
        expiration_minutes: int = 15,
    ) -> str:
        """Create a JWT access token with short expiration.
        
        Args:
            subject: Subject identifier (usually user_id or tenant_id)
            expiration_minutes: Token lifetime in minutes (default 15)
            
        Returns:
            Encoded JWT token string
            
        Raises:
            RuntimeError: If JWT secret key is missing or invalid
            
        Examples:
            >>> token = SecurityUtils.create_access_token("user_123")
            >>> assert len(token) > 100
            >>> assert "." in token  # Three parts separated by dots
        """
        now = datetime.now(timezone.utc)
        expire = now + timedelta(minutes=expiration_minutes)
        
        payload = {
            "sub": subject,
            "exp": expire,
            "iat": now,
            "type": "access",
        }
        
        # Get JWT secret from settings (validated on startup)
        secret_key = settings.jwt_secret
        
        if not secret_key:
            raise RuntimeError(
                "JWT secret key is not configured. Please set JWT_SECRET environment variable"
            )
        
        algorithm = settings.jwt_algorithm.upper()
        
        try:
            token = pyjwt.encode(payload, secret_key, algorithm=algorithm)
            logger.debug(f"Access token created for subject: {subject[:4]}...")
            return token
        except Exception as e:
            logger.error(f"Failed to create access token: {e}")
            raise
    
    @staticmethod
    def create_refresh_token(subject: str) -> str:
        """Create a JWT refresh token with longer expiration.
        
        Args:
            subject: Subject identifier
            expiration_days: Token lifetime in days (default 7)
            
        Returns:
            Encoded JWT refresh token string
        """
        now = datetime.now(timezone.utc)
        expire = now + timedelta(days=7)
        
        payload = {
            "sub": subject,
            "exp": expire,
            "iat": now,
            "type": "refresh",
        }
        
        secret_key = settings.jwt_refresh_secret
        
        if not secret_key:
            raise RuntimeError("JWT refresh secret key is not configured")
        
        try:
            token = pyjwt.encode(payload, secret_key, algorithm=settings.jwt_algorithm)
            return token
        except Exception as e:
            logger.error(f"Failed to create refresh token: {e}")
            raise
    
    @staticmethod
    def decode_access_token(token: str) -> dict:
        """Decode and validate an access token.
        
        Args:
            token: JWT access token string
            
        Returns:
            Decoded payload dictionary with 'sub', 'exp', 'iat', 'type' keys
            
        Raises:
            JWTError: If token is invalid, expired, or tampered
            ValueError: If token type is not 'access'
        """
        try:
            payload = pyjwt.decode(
                token,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )
            
            # Verify token type
            if payload.get("type") != "access":
                raise ValueError("Invalid token type: expected 'access'")
            
            return payload
        except JWTError as e:
            logger.warning(f"Invalid access token: {e}")
            raise
    
    @staticmethod
    def decode_refresh_token(token: str) -> dict:
        """Decode and validate a refresh token.
        
        Args:
            token: JWT refresh token string
            
        Returns:
            Decoded payload dictionary
            
        Raises:
            JWTError: If token is invalid or expired
        """
        try:
            payload = pyjwt.decode(
                token,
                settings.jwt_refresh_secret,
                algorithms=[settings.jwt_algorithm],
            )
            
            if payload.get("type") != "refresh":
                raise ValueError("Invalid token type: expected 'refresh'")
            
            return payload
        except JWTError as e:
            logger.warning(f"Invalid refresh token: {e}")
            raise
    
    @staticmethod
    def get_expiry_from_token(token: str) -> Optional[datetime]:
        """Extract expiration time from an unverified token.
        
        Useful for showing "token expires in X minutes" without decoding fully.
        
        Args:
            token: JWT token string
            
        Returns:
            Expiration datetime or None if parsing fails
        """
        try:
            payload = pyjwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=[settings.jwt_algorithm],
            )
            exp_timestamp = payload.get("exp")
            
            if exp_timestamp:
                return datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)
            return None
        except Exception as e:
            logger.error(f"Could not extract expiry from token: {e}")
            return None


class FernetCrypto:
    """Fernet symmetric encryption wrapper for sensitive data."""
    
    _instance: Optional["FernetCrypto"] = None
    _encrypted: bool = False
    
    def __new__(cls) -> "FernetCrypto":
        """Singleton pattern to ensure single Fernet instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """Initialize Fernet instance with encrypted API key from settings."""
        if not self._encrypted and settings.api_key_fernet:
            self.cipher = Fernet(settings.api_key_fernet)
            self._encrypted = True
    
    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext using Fernet symmetric encryption.
        
        Args:
            plaintext: Raw bytes to encrypt
            
        Returns:
            Base64-encoded ciphertext with authentication tag
            
        Examples:
            >>> crypto = FernetCrypto()
            >>> encrypted = crypto.encrypt(b"secret_api_key")
            >>> assert len(encrypted) > 0
        """
        if not self._encrypted:
            raise RuntimeError("Fernet key not initialized")
        
        return self.cipher.encrypt(plaintext)
    
    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt ciphertext using Fernet symmetric encryption.
        
        Args:
            ciphertext: Base64-encoded encrypted data
            
        Returns:
            Original plaintext bytes
            
        Raises:
            Exception: If decryption fails (invalid key, tampered data)
        """
        if not self._encrypted:
            raise RuntimeError("Fernet key not initialized")
        
        try:
            return self.cipher.decrypt(ciphertext)
        except Exception as e:
            logger.error(f"Fernet decryption failed: {e}")
            raise
    
    def encrypt_string(self, plaintext: str) -> str:
        """Encrypt a string and return base64-encoded result.
        
        Convenience method for string inputs.
        
        Args:
            plaintext: String to encrypt
            
        Returns:
            Base64-encoded encrypted string
        """
        encrypted_bytes = self.encrypt(plaintext.encode('utf-8'))
        return encrypted_bytes.decode('utf-8')
    
    def decrypt_string(self, ciphertext: str) -> str:
        """Decrypt a base64-encoded string and return original text.
        
        Convenience method for string outputs.
        
        Args:
            ciphertext: Base64-encoded encrypted string
            
        Returns:
            Decrypted plaintext string
        """
        decrypted_bytes = self.decrypt(ciphertext.encode('utf-8'))
        return decrypted_bytes.decode('utf-8')


def get_current_user_from_jwt(jwt_token: str) -> dict:
    """Extract current user info from JWT token.
    
    This is a convenience function often used in API dependencies.
    
    Args:
        jwt_token: JWT bearer token string (without "Bearer " prefix)
        
    Returns:
        Dictionary with user identification data
        
    Raises:
        JWTError: If token is invalid or expired
    """
    payload = SecurityUtils.decode_access_token(jwt_token)
    
    return {
        "user_id": payload.get("sub"),
        "token_type": payload.get("type"),
        "issued_at": datetime.fromtimestamp(
            payload.get("iat", 0), 
            tz=timezone.utc
        ),
        "expires_at": datetime.fromtimestamp(
            payload.get("exp", 0),
            tz=timezone.utc
        ),
    }


# Export public API
__all__ = [
    "SecurityUtils",
    "FernetCrypto",
    "get_current_user_from_jwt",
    "pwd_context",
]
