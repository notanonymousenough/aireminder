import asyncio

from main import ReminderBot

tags = [
    {"name": "default", "start_time": "00:00", "end_time": "23:59"},
    {"name": "Работа", "start_time": "09:00", "end_time": "18:00"},
    {"name": "Отдых", "start_time": "18:00", "end_time": "23:00"},
]

cases = [
    "Напомни через 6 часов найти билет на концерт и заказать его",
    "Распечатать документы в 10 утра",
    "Прочитать сообщение от Никиты с утра и выполнить шаги из инструкции",
    'Напомни завтра "if (is_predicted) {do_not_predict("foo_bar")}"',
    "Напоминай мне каждые 2 минуты на протяжении получаса вставать с кровати",
    "Через полгода когда закончу учёбу надо будет заказать пиццу, завтра надо будет позаниматься спортом",
    "По работе задание сделать реализацию асинхронного обновления, сегодня нужно будет отдохнуть и позаниматься йогой, но не забыть купить шарики к др",
    "Напомни мне мильон раз про изучение английского!!!",
    "Раз 5 на протяжении утра про вынос мусора",
]

bot = ReminderBot()

async def test(case):
    res1 = await bot.ask_llm_extract(tags, case)
    res2 = await bot.ask_llm_plan(tags, res1, case)
    print(case)
    print(res1)
    print(res2)

for case in cases:
    asyncio.run(test(case))
