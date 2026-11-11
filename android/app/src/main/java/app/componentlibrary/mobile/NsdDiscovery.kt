package app.componentlibrary.mobile

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo

class NsdDiscovery(context: Context) {
    private val manager = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private var listener: NsdManager.DiscoveryListener? = null

    fun start(serverId: String, found: (String, Int) -> Unit) {
        stop()
        listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) = Unit
            override fun onDiscoveryStopped(serviceType: String) = Unit
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) = stop()
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) = Unit
            override fun onServiceLost(serviceInfo: NsdServiceInfo) = Unit
            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                if (!serviceInfo.serviceName.startsWith(serverId)) return
                manager.resolveService(serviceInfo, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) = Unit
                    @Suppress("DEPRECATION")
                    override fun onServiceResolved(info: NsdServiceInfo) {
                        found(info.host.hostAddress ?: return, info.port)
                    }
                })
            }
        }
        runCatching {
            // DNS-SD 服务类型标签最多 15 字节，需与桌面端的短名称保持一致。
            manager.discoverServices("_componentlib._tcp.", NsdManager.PROTOCOL_DNS_SD, listener)
        }.onFailure { listener = null }
    }

    fun stop() {
        listener?.let { runCatching { manager.stopServiceDiscovery(it) } }
        listener = null
    }
}
