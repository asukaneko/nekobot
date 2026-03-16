# Ncatbot docker部署

> 环境：Debian10，Fnos

::: info
在docker上部署，可以分成两个容器，一个是napcat服务器，一个是ncatbot客户端，当然，你也可以在napcat容器中直接运行bot，你需要下载python和pip3，这个时候就不用配置webui地址和ws地址了（为127.0.0.1）
:::

## 一、安装Napcat docker版

使用ssh连接服务器或nas
``` bash
ssh user@yourip 
```

然后输入
```bash
curl -o \
napcat.sh \
https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh \
&& sudo bash napcat.sh
```

在命令行界面选择docker安装

## 二、配置Napcat

由于使用的是docker，不能直接使用localhost或127.0.0.1来连接，所以要先确认napcat的IP地址  

docker中需要有配置好的网络，比如我的是bridge  

![](/docker/image-zhiv.png)

在ssh中输入以查询napcat的IP地址
```bash
sudo docker inspect napcat
```
![](/docker/image.png)

在最后出现的IPAddress就是你需要的IP，我的是172.17.0.4

随后进入napcat webui，输入默认token，一般是napcat，后，登录qq，选择网络配置，新建一个websocket服务器

主机地址填上一步的IP地址或者0.0.0.0都可以

![](/docker/image-evbv.png)

![](/docker/image-reod.png)

## 三、配置Ncatbot

创建一个Ncatbot的容器，可以直接使用python的镜像，然后把你自己的ncatbot代码挂载到容器中

你需要额外配置

![](/docker/image-kqao.png) 

ws_url 为 ws://172.17.0.4:3001

主要要配置webui_uri和webui_token

这个webui_token默认为napcat，但是也可以在webui中修改为其他的

最后就可以连上了

![](/docker/image-njul.png)

::: warning
注意：如果想要实现上传文件功能，则务必保证该文件在napcat服务端可以被访问，即你也要把ncatbot的代码挂载到napcat容器中，并且代码的绝对位置在两个容器中要一致
:::
