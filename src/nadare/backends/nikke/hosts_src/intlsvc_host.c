/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 Nadare contributors
 *
 * intlsvc_host.c - start the installed INTL service without nikke_launcher.exe,
 * sign in from the saved session and write the resulting session file for the
 * game. It plays the launcher's part towards the service and nothing more.
 */
#define _WIN32_WINNT 0x0600
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <io.h>
#include <fcntl.h>

static FILE *g_log = NULL;
#define LOG(...) fprintf(g_log ? g_log : stdout, __VA_ARGS__)

static unsigned char buf[262144];
static size_t n;

static void put_varint(uint64_t v){ while(v>=0x80){ buf[n++]=(unsigned char)(v|0x80); v>>=7;} buf[n++]=(unsigned char)v; }
static void put_tag(int f,int wt){ put_varint(((uint64_t)f<<3)|wt); }
static void put_u64(int f,uint64_t v){ put_tag(f,0); put_varint(v); }
static void put_bytes(int f,const void*d,size_t len){ put_tag(f,2); put_varint(len); memcpy(buf+n,d,len); n+=len; }
static void put_str(int f,const char*s){ put_bytes(f,s,strlen(s)); }

static int read_exact(HANDLE h, void *p, DWORD len){
    DWORD got=0, total=0;
    while(total<len){
        if(!ReadFile(h,(char*)p+total,len-total,&got,NULL)) return -1;
        if(got==0) return -1;
        total+=got;
    }
    return 0;
}
static int write_exact(HANDLE h, const void *p, DWORD len){
    DWORD wrote=0, total=0;
    while(total<len){
        if(!WriteFile(h,(const char*)p+total,len-total,&wrote,NULL)) return -1;
        total+=wrote;
    }
    return 0;
}
static void send_msg(HANDLE h){
    unsigned char frame[512];
    uint32_t msglen=(uint32_t)n;
    frame[0]=msglen&0xff; frame[1]=(msglen>>8)&0xff; frame[2]=(msglen>>16)&0xff; frame[3]=(msglen>>24)&0xff;
    memcpy(frame+4,buf,msglen);
    LOG("send msg %u bytes\n",msglen);
    if(write_exact(h,frame,4+msglen)!=0) LOG("write failed err=%lu\n",GetLastError());
}
static unsigned char g_last[262144]; static size_t g_lastlen=0;
static int recv_msg(HANDLE h){
    unsigned char lenb[4]; DWORD got=0;
    if(!ReadFile(h,lenb,4,&got,NULL) || got!=4){ LOG("read len failed got=%lu err=%lu\n",got,GetLastError()); return -1; }
    uint32_t len=lenb[0]|(lenb[1]<<8)|(lenb[2]<<16)|((uint32_t)lenb[3]<<24);
    LOG("rsp len=%u\n",len);
    if(len==0||len>sizeof(buf)) return -1;
    unsigned char *rsp=malloc(len);
    if(read_exact(h,rsp,len)!=0){ LOG("read body failed err=%lu\n",GetLastError()); free(rsp); return -1; }
    if(len<=sizeof(g_last)){ memcpy(g_last,rsp,len); g_lastlen=len; }
    if(len<=64){ for(uint32_t i=0;i<len;i++) LOG("%02x",rsp[i]); LOG("\n"); }
    free(rsp);
    return 0;
}


static int pb_next(const unsigned char *b, size_t len, size_t *o, int *field, int *wt, unsigned long long *val, const unsigned char **data, size_t *dlen);
static int read_exact_timeout(HANDLE h, void *p, DWORD len, DWORD timeout_ms){
    DWORD total=0;
    ULONGLONG start=GetTickCount64();
    while(total<len){
        DWORD avail=0;
        if(!PeekNamedPipe(h,NULL,0,NULL,&avail,NULL)) return -1;
        if(avail>0){
            DWORD want=len-total; if(want>avail) want=avail;
            DWORD got=0;
            if(!ReadFile(h,(char*)p+total,want,&got,NULL)) return -1;
            if(got==0) return -1;
            total+=got;
        } else {
            if(GetTickCount64()-start>=timeout_ms) return -2;
            Sleep(20);
        }
    }
    return 0;
}
static int pb_field1(const unsigned char *b, size_t len, unsigned long long *out){
    size_t o=0; int f,wt; unsigned long long v=0; const unsigned char *d; size_t dl;
    while(pb_next(b,len,&o,&f,&wt,&v,&d,&dl)){ if(f==1 && wt==0){ *out=v; return 1; } }
    return 0;
}
/* Interactive mode: read request frames from stdin, write one response frame to stdout per
 * request. The caller can keep one intl_service process alive while a human reads an e-mailed
 * code. All human-readable output goes to stderr so stdout stays binary. */
static int stdio_mode(HANDLE h, DWORD rsp_timeout_ms){
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
    unsigned char lenb[4];
    unsigned char *req=malloc(262144), *rsp=malloc(262144), *wire=malloc(262148);
    int rc=0;
    if(!req||!rsp||!wire){ LOG("out of memory\n"); rc=1; goto done; }
    for(;;){
        size_t got=fread(lenb,1,4,stdin);
        if(got==0 && feof(stdin)) break;
        if(got!=4){ LOG("stdin short read\n"); rc=1; break; }
        uint32_t len=lenb[0]|(lenb[1]<<8)|(lenb[2]<<16)|((uint32_t)lenb[3]<<24);
        if(len==0||len>262144){ LOG("request length invalid: %u\n",len); rc=1; break; }
        if(fread(req,1,len,stdin)!=len){ LOG("stdin short message\n"); rc=1; break; }
        wire[0]=len&0xff; wire[1]=(len>>8)&0xff; wire[2]=(len>>16)&0xff; wire[3]=(len>>24)&0xff;
        memcpy(wire+4,req,len);
        if(write_exact(h,wire,4+len)!=0){ LOG("write failed err=%lu\n",GetLastError()); rc=1; break; }
        DWORD wr=read_exact_timeout(h,lenb,4,rsp_timeout_ms);
        if(wr==-2){ LOG("TIMEOUT no response to this request (continuing)\n"); continue; }
        if(wr!=0){ LOG("read len failed err=%lu\n",GetLastError()); rc=1; break; }
        uint32_t rlen=lenb[0]|(lenb[1]<<8)|(lenb[2]<<16)|((uint32_t)lenb[3]<<24);
        if(rlen==0||rlen>262144){ LOG("response length invalid: %u\n",rlen); rc=1; break; }
        wr=read_exact_timeout(h,rsp,rlen,rsp_timeout_ms);
        if(wr!=0){ LOG("read body failed err=%lu timeout=%d\n",GetLastError(),wr==-2); rc=1; break; }
        unsigned long long send_cmd=0, rsp_cmd=0;
        pb_field1(req,len,&send_cmd);
        pb_field1(rsp,rlen,&rsp_cmd);
        if(fwrite(lenb,1,4,stdout)!=4 || fwrite(rsp,1,rlen,stdout)!=rlen){
            LOG("write stdout failed\n"); rc=1; break;
        }
        fflush(stdout);
        LOG("send cmd=%llu rsp cmd=%llu bytes=%u\n",send_cmd,rsp_cmd,rlen);
    }
done:
    free(req); free(rsp); free(wire);
    return rc;
}

static int pb_next(const unsigned char *b, size_t len, size_t *o, int *field, int *wt, unsigned long long *val, const unsigned char **data, size_t *dlen){
    if(*o>=len) return 0;
    unsigned long long tag=0; int sh=0;
    while(*o<len){ unsigned char c=b[(*o)++]; tag|=(unsigned long long)(c&0x7f)<<sh; if(!(c&0x80)) break; sh+=7; }
    *field=(int)(tag>>3); *wt=(int)(tag&7);
    if(*wt==0){ unsigned long long v=0; sh=0; while(*o<len){ unsigned char c=b[(*o)++]; v|=(unsigned long long)(c&0x7f)<<sh; if(!(c&0x80)) break; sh+=7; } *val=v; return 1; }
    if(*wt==2){ unsigned long long l=0; sh=0; while(*o<len){ unsigned char c=b[(*o)++]; l|=(unsigned long long)(c&0x7f)<<sh; if(!(c&0x80)) break; sh+=7; } if(*o+l>len) l=len-*o; *data=b+*o; *dlen=(size_t)l; *o+=(size_t)l; return 1; }
    return 0;
}
static int extract_auth(const unsigned char *msg, size_t len, const unsigned char **out, size_t *outlen){
    size_t o=0; int f,wt; unsigned long long v; const unsigned char *d; size_t dl;
    const unsigned char *auth=NULL; size_t al=0;
    while(pb_next(msg,len,&o,&f,&wt,&v,&d,&dl)){ if(f==8 && wt==2){ auth=d; al=dl; } }
    if(!auth) return 0;
    o=0;
    while(pb_next(auth,al,&o,&f,&wt,&v,&d,&dl)){ if(f==21 && wt==2){ *out=d; *outlen=dl; return 1; } }
    return 0;
}

int main(int argc,char**argv){
    setvbuf(stdout,NULL,_IONBF,0);
    unsigned long long gameid = 29080;
    const char *svc = "S:\\intl_service\\intl_service.exe";
    const char *out = "session.enc.json";
    const char *parent = "nikke_launcher";
    int start_from = 0;
    int login_channel = 6;
    int auto_login = 1;
    int stdio = 0;
    DWORD rsp_timeout_ms = 20000;
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--service") == 0 && i + 1 < argc) svc = argv[++i];
        else if (strcmp(argv[i], "--out") == 0 && i + 1 < argc) out = argv[++i];
        else if (strcmp(argv[i], "--parent-name") == 0 && i + 1 < argc) parent = argv[++i];
        else if (strcmp(argv[i], "--start-from") == 0 && i + 1 < argc) start_from = atoi(argv[++i]);
        else if (strcmp(argv[i], "--game-id") == 0 && i + 1 < argc) gameid = strtoull(argv[++i], 0, 10);
        else if (strcmp(argv[i], "--login-channel") == 0 && i + 1 < argc) login_channel = atoi(argv[++i]);
        else if (strcmp(argv[i], "--auto-login") == 0 && i + 1 < argc) auto_login = atoi(argv[++i]);
        else if (strcmp(argv[i], "--stdio") == 0) stdio = 1;
        else if (strcmp(argv[i], "--rsp-timeout-ms") == 0 && i + 1 < argc) rsp_timeout_ms = (DWORD)strtoul(argv[++i], 0, 10);
        else { LOG("unknown argument: %s\n", argv[i]); return 2; }
    }
    g_log = stdio ? stderr : stdout;
    char workdir[MAX_PATH];
    snprintf(workdir, sizeof workdir, "%s", svc);
    char *slash = strrchr(workdir, '\\');
    if (slash) *slash = 0; else snprintf(workdir, sizeof workdir, ".");

    DWORD pid=GetCurrentProcessId();
    char pipe[256]; snprintf(pipe,sizeof(pipe),"\\\\.\\pipe\\rail_base_pipeintl_service_pipe%lu",(unsigned long)pid);
    LOG("pid=%lu pipe=%s\n",(unsigned long)pid,pipe);
    HANDLE srv=CreateNamedPipeA(pipe,PIPE_ACCESS_DUPLEX,PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT,1,262144,262144,0,NULL);
    if(srv==INVALID_HANDLE_VALUE){ LOG("CreateNamedPipe failed err=%lu\n",GetLastError()); return 1; }
    char cmd[1024]; snprintf(cmd,sizeof(cmd),"\"%s\" --LauncherPID=%lu --ParentName=%s --StartFrom=%d",svc,(unsigned long)pid,parent,start_from);
    STARTUPINFOA si; PROCESS_INFORMATION pi; memset(&si,0,sizeof si); si.cb=sizeof si;
    LOG("spawn %s (cwd %s)\n",cmd,workdir);
    if(!CreateProcessA(NULL,cmd,NULL,NULL,FALSE,0,NULL,workdir,&si,&pi)){ LOG("CreateProcess failed err=%lu\n",GetLastError()); return 1; }
    LOG("service pid=%lu\n",(unsigned long)pi.dwProcessId);
    LOG("waiting for connect...\n");
    if(!ConnectNamedPipe(srv,NULL)){ DWORD e=GetLastError(); if(e!=ERROR_PIPE_CONNECTED){ LOG("ConnectNamedPipe failed err=%lu\n",e); return 1; } }
    LOG("connected\n");

    if (stdio) {
        /* stdout is binary from here on; one fixed marker tells the caller the service connected. */
        fflush(g_log);
        _setmode(_fileno(stdout), _O_BINARY);
        fputs("NADARE-READY\n", stdout);
        fflush(stdout);
        return stdio_mode(srv, rsp_timeout_ms);
    }

    /* InitReq */
    n=0; put_u64(1,gameid); put_str(2,"0123456789abcdef0123456789abcdef"); put_u64(3,1033);
    size_t initlen=n; unsigned char init[512]; memcpy(init,buf,initlen);
    n=0; put_u64(1,1); put_u64(3,1); put_bytes(6,init,initlen);
    send_msg(srv);
    if(recv_msg(srv)!=0) return 1;
    /* AccountLoginReq: login_channel=6 (Google), auto_login=1 */
    n=0; put_u64(1,(uint64_t)login_channel); put_u64(3,(uint64_t)auto_login);
    size_t lrlen=n; unsigned char lr[256]; memcpy(lr,buf,lrlen);
    n=0; put_u64(1,301); put_u64(3,2); put_bytes(7,lr,lrlen);
    send_msg(srv);
    if(recv_msg(srv)!=0) LOG("login recv failed\n");
    const unsigned char *auth=NULL; size_t alen=0;
    if(extract_auth(g_last,g_lastlen,&auth,&alen)){
        LOG("auth_encypt_data len=%zu\n",alen);
        FILE *f=fopen(out,"wb"); if(f){ fwrite(auth,1,alen,f); fclose(f); LOG("wrote %s\n",out); } else LOG("open %s failed err=%lu\n",out,GetLastError());
    } else LOG("auth_encypt_data not found in last response (len=%zu)\n",g_lastlen);
    LOG("done, service pid %lu still running\n",(unsigned long)pi.dwProcessId);
    return 0;
}
