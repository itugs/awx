from awx.main.models import Instance


def get_local_queuename():
    return Instance.objects.me().hostname.encode('utf-8')
