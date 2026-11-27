"""品种元数据：板块分组、合约月份规则、基本面关键变量与否决权变量。

关键变量与否决权变量提炼自 futures-fundamental-research skill 的
references/*-playbook.md，用于前端基本面工作台动态生成研究清单。
"""

# code: 新浪合约代码前缀（连续主力用 <code>0）
# months: 该品种实际挂牌的交割月份
# vars: 关键变量（研究清单）
# veto: 否决权变量（1-3 个，足以推翻主叙事）
# anchor: 强制外盘锚定的品种
# exchange: 交易所

BLACK = "黑色"
NONFERROUS = "有色"
ENERGY = "能源化工"
AGRI = "农产品"
SOFT = "软商品小品种"
PRECIOUS = "贵金属"

ALL_MONTHS = list(range(1, 13))

SYMBOLS = [
    # ---------------- 黑色 ----------------
    {
        "code": "RB", "name": "螺纹钢", "exchange": "SHFE", "group": BLACK,
        "months": [1, 5, 10],
        "vars": ["铁水产量", "五大材产量", "五大材表需", "总库存(厂库/社库)", "高炉开工率", "电炉开工", "港口库存", "现货成交"],
        "veto": ["铁水产量", "五大材表需", "总库存去化速度"],
        "anchor": False,
        "note": "黑色研究中枢是铁水，宏观叙事必须接受铁水、表需、库存检验",
    },
    {
        "code": "HC", "name": "热卷", "exchange": "SHFE", "group": BLACK,
        "months": [1, 5, 10],
        "vars": ["铁水产量", "五大材表需", "总库存", "钢厂利润", "制造业PMI", "出口订单"],
        "veto": ["铁水产量", "五大材表需", "库存去化速度"],
        "anchor": False,
        "note": "制造业需求为主线，看板材供需与出口",
    },
    {
        "code": "I", "name": "铁矿石", "exchange": "DCE", "group": BLACK,
        "months": ALL_MONTHS,
        "vars": ["港口库存", "发运量", "到港量", "铁水产量", "钢厂利润", "块矿溢价", "基差"],
        "veto": ["铁水产量", "港口库存", "钢厂利润"],
        "anchor": True,
        "note": "内外盘联动强，新加坡掉期需对照",
    },
    {
        "code": "JM", "name": "焦煤", "exchange": "DCE", "group": BLACK,
        "months": ALL_MONTHS,
        "vars": ["原煤产量", "洗煤厂开工", "蒙煤通关车数", "钢厂焦煤库存", "焦化利润", "港口库存"],
        "veto": ["蒙煤通关量", "钢厂焦煤库存", "焦化开工"],
        "anchor": False,
        "note": "供给弹性由进口主导，注意通关节奏",
    },
    {
        "code": "J", "name": "焦炭", "exchange": "DCE", "group": BLACK,
        "months": ALL_MONTHS,
        "vars": ["焦化产能利用率", "焦炭产量", "钢厂焦炭库存", "焦化利润", "环保限产"],
        "veto": ["焦化开工", "钢厂焦炭库存", "铁水产量"],
        "anchor": False,
        "note": "与铁水强绑定，注意去库速度",
    },
    {
        "code": "SF", "name": "硅铁", "exchange": "CZCE", "group": BLACK,
        "months": ALL_MONTHS,
        "vars": ["兰炭产量", "硅铁厂开工", "现货成交", "库存", "钢招量", "新能源需求"],
        "veto": ["钢招量", "硅铁厂开工", "库存"],
        "anchor": False,
        "note": "钢招定价 + 季节性检修，交割口径敏感",
    },
    {
        "code": "SM", "name": "硅锰", "exchange": "CZCE", "group": BLACK,
        "months": ALL_MONTHS,
        "vars": ["锰矿进口", "硅锰厂开工", "钢招量", "库存", "电耗成本"],
        "veto": ["钢招量", "硅锰厂开工", "库存"],
        "anchor": False,
        "note": "成本端锰矿与钢招双向定价",
    },
    # ---------------- 有色 ----------------
    {
        "code": "CU", "name": "铜", "exchange": "SHFE", "group": NONFERROUS,
        "months": ALL_MONTHS,
        "vars": ["LME库存", "SHFE库存", "社会库存", "保税区库存", "现货升贴水", "进口窗口", "TC/RC", "冶炼利润"],
        "veto": ["三地库存", "现货升贴水", "进口窗口"],
        "anchor": True,
        "note": "全球定价品种，宏观与微观冲突时以库存和现货为准",
    },
    {
        "code": "AL", "name": "铝", "exchange": "SHFE", "group": NONFERROUS,
        "months": ALL_MONTHS,
        "vars": ["电解铝产量", "运行产能", "社会库存", "现货升贴水", "进口窗口", "铝棒加工费", "电价"],
        "veto": ["社会库存", "现货升贴水", "进口窗口"],
        "anchor": True,
        "note": "供给天花板附近，盯产量与进口",
    },
    {
        "code": "ZN", "name": "锌", "exchange": "SHFE", "group": NONFERROUS,
        "months": ALL_MONTHS,
        "vars": ["精炼锌产量", "社会库存", "现货升贴水", "进口窗口", "镀锌开工", "锌精矿到港"],
        "veto": ["社会库存", "进口窗口", "锌精矿供应"],
        "anchor": True,
        "note": "矿端紧张与冶炼过剩经常背离",
    },
    {
        "code": "PB", "name": "铅", "exchange": "SHFE", "group": NONFERROUS,
        "months": ALL_MONTHS,
        "vars": ["原生铅产量", "再生铅供应", "社会库存", "升贴水", "旺季需求", "废铅价格"],
        "veto": ["社会库存", "再生铅供应", "旺季需求"],
        "anchor": False,
        "note": "再生铅占比高，成本支撑逻辑特殊",
    },
    {
        "code": "NI", "name": "镍", "exchange": "SHFE", "group": NONFERROUS,
        "months": ALL_MONTHS,
        "vars": ["LME镍库存", "国内社会库存", "镍矿价格", "NPI价格", "镍铁产量", "硫酸镍产量", "不锈钢排产"],
        "veto": ["镍铁产量", "社会库存", "不锈钢排产"],
        "anchor": True,
        "note": "印尼供给主导，结构性过剩风险高",
    },
    {
        "code": "SN", "name": "锡", "exchange": "SHFE", "group": NONFERROUS,
        "months": ALL_MONTHS,
        "vars": ["缅甸矿进口", "国内冶炼产量", "社会库存", "现货升贴水", "焊锡开工", "电子需求"],
        "veto": ["社会库存", "缅甸矿供应", "焊锡开工"],
        "anchor": True,
        "note": "供给扰动频繁，弹性极大",
    },
    {
        "code": "AO", "name": "氧化铝", "exchange": "SHFE", "group": NONFERROUS,
        "months": ALL_MONTHS,
        "vars": ["矿石价格", "氧化铝产量", "电解铝需求", "现货升贴水", "港口库存"],
        "veto": ["现货升贴水", "港口库存", "电解铝开工"],
        "anchor": False,
        "note": "紧跟铝产业链，成本传导敏感",
    },
    {
        "code": "SS", "name": "不锈钢", "exchange": "SHFE", "group": NONFERROUS,
        "months": ALL_MONTHS,
        "vars": ["产量", "库存", "镍铬成本", "表观消费", "出口"],
        "veto": ["产量", "库存", "镍价成本"],
        "anchor": False,
        "note": "产业链利润在钢厂与镍铬之间分配",
    },
    # ---------------- 能源化工 ----------------
    {
        "code": "SC", "name": "原油", "exchange": "INE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["EIA库存", "OPEC产量", "Brent/WTI曲线", "裂解价差", "炼厂开工", "中国进口", "汇率"],
        "veto": ["海外库存", "OPEC供给", "中国需求"],
        "anchor": True,
        "note": "必须启用外盘锚定层，近月交割风险高",
    },
    {
        "code": "FU", "name": "燃料油", "exchange": "INE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["高硫燃料油产量", "船燃需求", "FU裂解价差", "港口库存", "合规成本"],
        "veto": ["船燃需求", "港口库存", "裂解价差"],
        "anchor": True,
        "note": "跟随原油但受船燃与制裁结构影响",
    },
    {
        "code": "BU", "name": "沥青", "exchange": "SHFE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["炼厂开工", "沥青产量", "道路开工率", "社会库存", "稀释沥青利润"],
        "veto": ["道路开工", "社会库存", "炼厂开工"],
        "anchor": True,
        "note": "原油成本 + 基建需求双驱动，季节性极强",
    },
    {
        "code": "TA", "name": "PTA", "exchange": "CZCE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["PTA装置检修", "PX价差", "PTA加工费", "聚酯开工", "聚酯库存", "社会库存"],
        "veto": ["聚酯开工", "PTA加工费", "社会库存"],
        "anchor": True,
        "note": "加工费是核心矛盾，检修要等兑现",
    },
    {
        "code": "EG", "name": "乙二醇", "exchange": "DCE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["乙二醇产量", "进口量", "港口库存", "聚酯开工", "煤制利润", "乙二醇-聚酯价差"],
        "veto": ["港口库存", "聚酯开工", "进口量"],
        "anchor": True,
        "note": "进口依存度高，跟踪到港节奏",
    },
    {
        "code": "MA", "name": "甲醇", "exchange": "CZCE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["甲醇装置开工", "港口库存", "MTO利润", "MTO开工", "进口量", "传统下游开工"],
        "veto": ["港口库存", "MTO利润与开工", "装置开工"],
        "anchor": True,
        "note": "MTO 利润是需求核心，传统下游只看边际",
    },
    {
        "code": "V", "name": "PVC", "exchange": "DCE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["PVC产量", "社会库存", "出口", "电石成本", "原油比价", "下游地板开工"],
        "veto": ["社会库存", "出口", "电石成本"],
        "anchor": True,
        "note": "高库存与出口退税博弈常主导",
    },
    {
        "code": "L", "name": "聚乙烯", "exchange": "DCE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["石化检修", "产量", "库存", "进口利润", "农膜需求", "油煤比"],
        "veto": ["库存", "进口利润", "农膜需求"],
        "anchor": True,
        "note": "油煤比定价，进口货源是关键变量",
    },
    {
        "code": "PP", "name": "聚丙烯", "exchange": "DCE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["PDH利润", "油制利润", "产量", "库存", "拉丝排产"],
        "veto": ["库存", "PDH利润", "拉丝排产"],
        "anchor": True,
        "note": "利润决定开工，注意负加工费减产",
    },
    {
        "code": "SA", "name": "纯碱", "exchange": "CZCE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["纯碱产量", "检修损失", "厂库", "社会库存", "浮法玻璃冷修", "光伏玻璃日熔量"],
        "veto": ["厂库与社会库存", "玻璃冷修", "光伏日熔量"],
        "anchor": False,
        "note": "光伏新增产能是长期压制项，近月看厂库",
    },
    {
        "code": "FG", "name": "玻璃", "exchange": "CZCE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["日熔量", "冷修产线", "厂库", "社会库存", "地产竣工", "光伏玻璃"],
        "veto": ["日熔量", "厂库", "地产竣工"],
        "anchor": False,
        "note": "纯需求品种，盯地产竣工与冷修节奏",
    },
    {
        "code": "UR", "name": "尿素", "exchange": "CZCE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["日产量", "开工率", "厂库", "农业需求", "出口", "煤炭成本"],
        "veto": ["日产量与开工", "厂库", "出口政策"],
        "anchor": False,
        "note": "供给弹性强，出口是边际变量",
    },
    {
        "code": "RU", "name": "天然橡胶", "exchange": "SHFE", "group": ENERGY,
        "months": [1, 3, 5, 7, 9, 11],
        "vars": ["原料胶价格", "全钢胎开工", "半钢胎开工", "青岛库存", "轮胎出口", "割胶季供应"],
        "veto": ["全钢胎开工", "青岛库存", "原料供应"],
        "anchor": True,
        "note": "季节性极强，供应旺季与需求旺季错位",
    },
    {
        "code": "SP", "name": "纸浆", "exchange": "SHFE", "group": ENERGY,
        "months": ALL_MONTHS,
        "vars": ["纸浆进口量", "纸浆厂开工", "纸厂库存", "文化纸价格", "生活用纸开工", "纸价提涨落地"],
        "veto": ["纸厂库存", "进口量", "提涨落地情况"],
        "anchor": True,
        "note": "高价警惕下游观望，盯提涨落地",
    },
    # ---------------- 农产品 ----------------
    {
        "code": "M", "name": "豆粕", "exchange": "DCE", "group": AGRI,
        "months": [1, 3, 5, 7, 8, 9, 11, 12],
        "vars": ["USDA平衡表", "美豆压榨", "巴西到港", "油厂豆粕库存", "提货基差", "养殖利润", "产区天气"],
        "veto": ["USDA实际调整量", "压榨与到港兑现", "库存消费比"],
        "anchor": True,
        "note": "报告与天气交易，注意旧作新作切换",
    },
    {
        "code": "Y", "name": "豆油", "exchange": "DCE", "group": AGRI,
        "months": [1, 3, 5, 7, 8, 9, 11, 12],
        "vars": ["豆油进口", "油厂开机", "豆油库存", "棕油产地库存", "菜油库存", "USDA油脂平衡表", "原油比价"],
        "veto": ["豆油库存", "棕油产地库存", "进口节奏"],
        "anchor": True,
        "note": "三大油脂相互替代，跨品种强弱比价关键",
    },
    {
        "code": "P", "name": "棕榈油", "exchange": "DCE", "group": AGRI,
        "months": ALL_MONTHS,
        "vars": ["MPOB产量", "马来库存", "出口数据", "印尼政策", "国内库存", "豆棕价差"],
        "veto": ["马来库存", "出口与政策", "豆棕价差"],
        "anchor": True,
        "note": "政策与产量驱动强，季节性明显",
    },
    {
        "code": "RM", "name": "菜粕", "exchange": "CZCE", "group": AGRI,
        "months": [1, 3, 5, 7, 8, 9, 11, 12],
        "vars": ["加拿大油菜籽产量", "菜籽到港", "油厂开机", "菜粕库存", "水产旺季", "RM-ZC价差"],
        "veto": ["到港节奏", "菜粕库存", "水产需求"],
        "anchor": True,
        "note": "加菜籽政策与水产需求双驱动",
    },
    {
        "code": "OI", "name": "菜油", "exchange": "CZCE", "group": AGRI,
        "months": [1, 3, 5, 7, 8, 9, 11, 12],
        "vars": ["菜油库存", "进口利润", "压榨量", "菜油-豆油价差", "油脂总库存"],
        "veto": ["菜油库存", "进口利润", "油脂总库存"],
        "anchor": True,
        "note": "跟随油脂板块但更依赖进口",
    },
    {
        "code": "C", "name": "玉米", "exchange": "DCE", "group": AGRI,
        "months": [1, 3, 5, 7, 8, 9, 11, 12],
        "vars": ["新玉米上市", "深加工开机", "港口库存", "猪料需求", "政策拍卖", "DDGS价差"],
        "veto": ["政策拍卖", "港口与深加工库存", "新粮上市节奏"],
        "anchor": False,
        "note": "政策与售粮进度主导，替代品联动",
    },
    {
        "code": "CS", "name": "玉米淀粉", "exchange": "DCE", "group": AGRI,
        "months": ALL_MONTHS,
        "vars": ["深加工开机率", "淀粉厂库存", "玉米库存", "现货成交", "CS-C价差"],
        "veto": ["深加工开机", "厂库", "玉米现货"],
        "anchor": False,
        "note": "加工利润驱动，贴水升水随原料波动",
    },
    {
        "code": "SR", "name": "白糖", "exchange": "CZCE", "group": AGRI,
        "months": ALL_MONTHS,
        "vars": ["巴西压榨", "印度出口", "国内销糖", "食糖库存", "配糖进口", "糖厂开工"],
        "veto": ["国内库存", "巴西压榨进度", "配糖进口"],
        "anchor": True,
        "note": "外盘定价+国内供需双轨，注意内外价差",
    },
    {
        "code": "CF", "name": "棉花", "exchange": "CZCE", "group": AGRI,
        "months": ALL_MONTHS,
        "vars": ["美棉出口", "USDA库存", "新疆棉花产量", "纺织开工", "棉花库存", "轮储拍卖"],
        "veto": ["USDA实际调整", "纺织开工", "库存"],
        "anchor": True,
        "note": "政策与美棉主导，纺织需求是慢变量",
    },
    {
        "code": "AP", "name": "苹果", "exchange": "CZCE", "group": AGRI,
        "months": [1, 3, 5, 7, 8, 9, 11, 12],
        "vars": ["产区天气", "套袋产量", "优果率", "冷库库存", "苹果期货交割", "批发成交"],
        "veto": ["冷库库存", "优果率", "产区天气"],
        "anchor": False,
        "note": "交割口径与品级敏感，区域差异大",
    },
    {
        "code": "CJ", "name": "红枣", "exchange": "CZCE", "group": AGRI,
        "months": [1, 3, 5, 7, 8, 9, 11, 12],
        "vars": ["产区种植", "原料收购价", "枣农持货", "下游成交", "沧州库存"],
        "veto": ["原料收购", "持货情绪", "下游成交"],
        "anchor": False,
        "note": "资金博弈属性强，交割标准宽松",
    },
    {
        "code": "LH", "name": "生猪", "exchange": "DCE", "group": AGRI,
        "months": ALL_MONTHS,
        "vars": ["能繁母猪存栏", "出栏均重", "标肥价差", "二次育肥", "猪价", "冻品库存"],
        "veto": ["能繁母猪存栏", "出栏节奏", "标肥价差"],
        "anchor": False,
        "note": "产能周期主导，节奏与体重是高频信号",
    },
    {
        "code": "JD", "name": "鸡蛋", "exchange": "DCE", "group": AGRI,
        "months": ALL_MONTHS,
        "vars": ["在产蛋鸡存栏", "鸡苗补栏", "淘汰鸡", "现货蛋价", "库存天数", "节日备货"],
        "veto": ["在产蛋鸡存栏", "现货蛋价", "补栏节奏"],
        "anchor": False,
        "note": "季节性强，合约切换有交割风险",
    },
    # ---------------- 贵金属 ----------------
    {
        "code": "AU", "name": "沪金", "exchange": "SHFE", "group": PRECIOUS,
        "months": ALL_MONTHS,
        "vars": ["COMEX库存", "ETF持仓", "美元指数", "实际利率", "央行购金", "沪金基差", "进口盈亏"],
        "veto": ["美元与实际利率", "外盘库存", "现货基差"],
        "anchor": True,
        "note": "宏观定价为主，外盘锚定强制启用",
    },
    {
        "code": "AG", "name": "沪银", "exchange": "SHFE", "group": PRECIOUS,
        "months": ALL_MONTHS,
        "vars": ["COMEX白银库存", "光伏银浆需求", "LBMA库存", "沪银基差", "内外价差", "金银比"],
        "veto": ["外盘库存", "光伏需求", "内外价差"],
        "anchor": True,
        "note": "工业属性叠加金融属性，双重驱动",
    },
]


# 说明：本工作台只覆盖国内期货品种。外盘行情数据未接入，
# 但内盘品种的 anchor 标记予以保留 —— 它表示该品种需人工核对
# 汇率、进口利润与内外价差（外盘锚定层），属于研究纪律而非行情依赖。

GROUPS = [BLACK, NONFERROUS, ENERGY, AGRI, SOFT, PRECIOUS]


def symbol_table() -> list[dict]:
    """返回全部品种的扁平表，附带 market 与连续主力代码。"""
    rows = []
    for item in SYMBOLS:
        row = dict(item)
        row["market"] = "domestic"
        row["continuous"] = f"nf_{item['code']}0"
        row["intl"] = False
        rows.append(row)
    return rows


def find_symbol(code: str) -> dict | None:
    for row in symbol_table():
        if row["code"].lower() == code.lower():
            return row
    return None
