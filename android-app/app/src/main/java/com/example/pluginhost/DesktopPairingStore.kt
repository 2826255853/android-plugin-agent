package com.example.pluginhost

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import org.json.JSONObject
import java.io.File
import java.io.FileNotFoundException
import java.security.KeyPairGenerator
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

internal class DesktopPairingStore(context: Context, private val namespace: String = "desktop-link-v1") {
    private val file = AtomicFile(File(context.noBackupFilesDir, namespace))
    private val keyStore get() = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    fun device(): DesktopDevice {
        val alias = "$namespace-device"
        val publicKey = keyStore.getCertificate(alias)?.publicKey ?: KeyPairGenerator
            .getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore").run {
                initialize(
                    KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
                        .setDigests(KeyProperties.DIGEST_SHA256)
                        .build(),
                )
                generateKeyPair().public
            }
        val fingerprint = MessageProtocol.sha256Hex(publicKey.encoded)
        return DesktopDevice("android-${fingerprint.take(48)}", fingerprint)
    }

    fun read(): DesktopPairing? {
        val encrypted = try {
            file.readFully()
        } catch (_: FileNotFoundException) {
            return null
        }
        val key = keyStore.getKey(namespace, null) as SecretKey
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, encrypted, 0, IV_BYTES))
        val plaintext = cipher.doFinal(encrypted, IV_BYTES, encrypted.size - IV_BYTES)
        val value = try {
            JSONObject(plaintext.toString(Charsets.UTF_8))
        } finally {
            plaintext.fill(0)
        }
        val endpoint = DesktopEndpoint.parse(value.getString("address"), value.getString("fingerprint"))
        if (value.has("blockedReason")) {
            return DesktopPairing(DesktopLink(endpoint, value.getString("blockedReason")), null)
        }
        val credential = DesktopCredential(value.getString("id"), value.getString("token"), value.getString("expiresAt"))
        return DesktopPairing(DesktopLink(endpoint), credential)
    }

    fun save(pairing: DesktopPairing) {
        val endpoint = pairing.link.endpoint
        val value = JSONObject().put("address", endpoint.address).put("fingerprint", endpoint.fingerprint)
        val credential = pairing.credential
        if (credential == null) {
            value.put("blockedReason", requireNotNull(pairing.link.blockedReason))
        } else {
            value.put("id", credential.id).put("token", credential.token).put("expiresAt", credential.expiresAt)
        }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, encryptionKey())
        val plaintext = value.toString().toByteArray(Charsets.UTF_8)
        val ciphertext = try {
            cipher.doFinal(plaintext)
        } finally {
            plaintext.fill(0)
        }
        val stream = file.startWrite()
        try {
            stream.write(cipher.iv)
            stream.write(ciphertext)
            file.finishWrite(stream)
        } catch (failure: Exception) {
            file.failWrite(stream)
            throw failure
        }
    }

    private fun encryptionKey(): SecretKey = keyStore.getKey(namespace, null) as? SecretKey
        ?: KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(
                KeyGenParameterSpec.Builder(namespace, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .build(),
            )
            generateKey()
        }

    private companion object {
        const val IV_BYTES = 12
    }
}
